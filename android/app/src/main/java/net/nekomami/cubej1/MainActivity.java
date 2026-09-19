package net.nekomami.cubej1;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * A single WebView pointed at meter_hub on the Cube.
 *
 * The dashboard is already a complete, self-refreshing page, so the app's
 * only jobs are to remember the address, show the page without browser
 * chrome, and say something useful when the Cube cannot be reached.
 *
 * The page is served over plain HTTP on the home LAN, which is why the
 * manifest sets usesCleartextTraffic. Chrome refuses to install such a page
 * as a PWA for the same reason; that refusal is the whole reason this app
 * exists.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "cubej1";
    private static final String KEY_HOST = "host";
    private static final String KEY_PORT = "port";
    private static final int DEFAULT_PORT = 8080;

    private WebView web;
    private LinearLayout setup;
    private TextView setupMessage;
    private EditText hostField;
    private EditText portField;
    private TextView urlPreview;

    /** Set by the error callback, cleared when a fresh load starts. */
    private boolean loadFailed;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(getColor(R.color.plane));

        web = new WebView(this);
        web.setBackgroundColor(getColor(R.color.plane));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.setWebViewClient(new Client());
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        root.addView(buildSetup(), new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);

        if (host().isEmpty()) {
            showSetup("Cube の IP アドレスを入れてください。");
        } else {
            load();
        }
    }

    // -- setup screen ------------------------------------------------------

    private LinearLayout buildSetup() {
        setup = new LinearLayout(this);
        setup.setOrientation(LinearLayout.VERTICAL);
        setup.setGravity(Gravity.CENTER);
        setup.setBackgroundColor(getColor(R.color.plane));
        int pad = dp(28);
        setup.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("電力モニタ");
        title.setTextColor(getColor(R.color.ink));
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 24);
        setup.addView(title);

        setupMessage = new TextView(this);
        setupMessage.setTextColor(getColor(R.color.ink2));
        setupMessage.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        setupMessage.setPadding(0, dp(10), 0, dp(24));
        setup.addView(setupMessage);

        hostField = new EditText(this);
        hostField.setHint("192.168.1.40  /  http://192.168.1.40:8080 でも可");
        hostField.setSingleLine(true);
        hostField.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostField.setTextColor(getColor(R.color.ink));
        setup.addView(hostField, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        portField = new EditText(this);
        portField.setHint("8080");
        portField.setSingleLine(true);
        portField.setInputType(InputType.TYPE_CLASS_NUMBER);
        portField.setTextColor(getColor(R.color.ink));
        setup.addView(portField, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        urlPreview = new TextView(this);
        urlPreview.setTextColor(getColor(R.color.ink2));
        urlPreview.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        urlPreview.setPadding(0, dp(10), 0, 0);
        setup.addView(urlPreview);

        TextWatcher watcher = new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence c, int a, int b, int d) { }
            @Override public void onTextChanged(CharSequence c, int a, int b, int d) { }
            @Override public void afterTextChanged(Editable e) { refreshPreview(); }
        };
        hostField.addTextChangedListener(watcher);
        portField.addTextChangedListener(watcher);

        Button connect = new Button(this);
        connect.setText("接続");
        connect.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                save();
                load();
            }
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(20);
        setup.addView(connect, lp);

        TextView note = new TextView(this);
        note.setText("ブラウザのアドレスバーからそのまま貼り付けても構いません。"
                + "Cube と同じ Wi-Fi につながっている必要があります。"
                + "IP はルーターの管理画面か find-cube.ps1 で調べられます。");
        note.setTextColor(getColor(R.color.ink2));
        note.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        note.setPadding(0, dp(20), 0, 0);
        setup.addView(note);

        setup.setVisibility(View.GONE);
        return setup;
    }

    /**
     * Pull a host and an optional port out of whatever was typed.
     *
     * The field asks for an address, but the obvious thing to paste is the
     * whole URL from the browser's address bar. Accept both rather than
     * building "http://http://10.0.0.2:8080:8080/" out of it.
     */
    static String[] parseHost(String raw) {
        String h = raw == null ? "" : raw.trim();
        int scheme = h.indexOf("://");
        if (scheme >= 0) {
            h = h.substring(scheme + 3);
        }
        int slash = h.indexOf('/');
        if (slash >= 0) {
            h = h.substring(0, slash);
        }
        String port = null;
        if (h.startsWith("[")) {              // [fe80::1]:8080
            int close = h.indexOf(']');
            if (close > 0) {
                String rest = h.substring(close + 1);
                h = h.substring(0, close + 1);
                if (rest.startsWith(":")) {
                    port = rest.substring(1);
                }
            }
        } else {
            int colon = h.indexOf(':');
            if (colon >= 0) {
                port = h.substring(colon + 1);
                h = h.substring(0, colon);
            }
        }
        if (port != null) {
            port = port.trim();
            if (port.isEmpty()) {
                port = null;
            }
        }
        return new String[]{h.trim(), port};
    }

    private void refreshPreview() {
        String[] parsed = parseHost(hostField.getText().toString());
        String h = parsed[0];
        String p = parsed[1] != null ? parsed[1] : portField.getText().toString().trim();
        if (p.isEmpty()) {
            p = String.valueOf(DEFAULT_PORT);
        }
        urlPreview.setText(h.isEmpty() ? "接続先: (未設定)" : "接続先: http://" + h + ":" + p + "/");
    }

    private void showSetup(String message) {
        setupMessage.setText(message);
        hostField.setText(host());
        portField.setText(String.valueOf(port()));
        refreshPreview();
        setup.setVisibility(View.VISIBLE);
    }

    // -- loading -----------------------------------------------------------

    private void load() {
        if (host().isEmpty()) {
            showSetup("Cube の IP アドレスを入れてください。");
            return;
        }
        loadFailed = false;
        web.loadUrl(url());
    }

    private String url() {
        return "http://" + host() + ":" + port() + "/";
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            return false; // the dashboard has no outbound links; keep it all in-app
        }

        @Override
        public void onPageStarted(WebView v, String u, android.graphics.Bitmap icon) {
            loadFailed = false;
        }

        @Override
        public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            if (!r.isForMainFrame()) {
                return; // a failed poll is not a failed page
            }
            loadFailed = true;
            showSetup("つながりません。\n\n" + url() + "\n"
                    + e.getErrorCode() + " " + e.getDescription() + "\n\n"
                    + "同じスマホのブラウザで上の URL が開けるなら、"
                    + "入力が違います。開けないなら Cube 側です。");
        }

        @Override
        public void onPageFinished(WebView v, String u) {
            if (!loadFailed) {
                setup.setVisibility(View.GONE);
            }
        }
    }

    // -- back button -------------------------------------------------------

    @Override
    public void onBackPressed() {
        if (setup.getVisibility() == View.VISIBLE) {
            super.onBackPressed();
            return;
        }
        if (web.canGoBack()) {
            web.goBack();
            return;
        }
        // No chrome means no obvious way back to the settings, so the back
        // button is the way in.
        new AlertDialog.Builder(this)
                .setTitle("電力モニタ")
                .setPositiveButton("閉じる", (d, which) -> finish())
                .setNeutralButton("接続先を変更", (d, which) ->
                        showSetup("接続先の IP とポートを変更できます。"))
                .setNegativeButton("キャンセル", null)
                .show();
    }

    // -- preferences -------------------------------------------------------

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private String host() {
        return prefs().getString(KEY_HOST, "").trim();
    }

    private int port() {
        return prefs().getInt(KEY_PORT, DEFAULT_PORT);
    }

    private void save() {
        String[] parsed = parseHost(hostField.getText().toString());
        String h = parsed[0];
        int p = DEFAULT_PORT;
        // A port pasted into the address field wins over the port box: it is
        // the more specific thing the person just typed.
        String raw = parsed[1] != null ? parsed[1] : portField.getText().toString().trim();
        if (!raw.isEmpty()) {
            try {
                int parsed = Integer.parseInt(raw);
                if (parsed >= 1 && parsed <= 65535) {
                    p = parsed;
                }
            } catch (NumberFormatException ignored) {
                // keep the default rather than refusing to move on
            }
        }
        prefs().edit().putString(KEY_HOST, h).putInt(KEY_PORT, p).apply();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
