package net.nekomami.cubej1;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
import android.widget.Toast;

import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Two pages in one WebView.
 *
 * At home the Cube's own dashboard is the live view and nothing here needs
 * to reproduce it. Away from home there is nothing to reach, so the app
 * shows its own page built from the history it has already pulled down.
 * Which one appears is decided by asking the Cube, not by reading the
 * Wi-Fi name, so the app needs no location permission.
 */
public class MainActivity extends Activity {

    private static final int REQ_CSV_SLOT = 101;
    private static final int REQ_CSV_MINUTE = 102;

    private static final String HISTORY_PAGE = "file:///android_asset/history.html";
    private static final String BRIDGE_NAME = "Meter";

    private WebView web;
    private LinearLayout setup;
    private TextView setupMessage;
    private EditText hostField;
    private EditText portField;
    private TextView urlPreview;

    private Store store;
    private Bridge bridge;
    private final Handler ui = new Handler(Looper.getMainLooper());

    /** Set by the error callback, cleared when a fresh load starts. */
    private boolean loadFailed;
    /** True while the bundled history page is showing. */
    private boolean offline;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        store = Store.get(this);
        bridge = new Bridge(this, store);

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
        // The history page lives in assets. Asset URLs are nominally exempt
        // from the file-access switch, but saying so outright costs nothing
        // and removes a per-version surprise.
        s.setAllowFileAccess(true);
        web.setWebViewClient(new Client());
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        root.addView(buildSetup(), new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);

        SyncJob.schedule(this);

        if (!Prefs.configured(this)) {
            showSetup("Cube の IP アドレスを入れてください。");
        } else {
            chooseView();
        }
    }

    // -- which page to show ------------------------------------------------

    /** Ask the Cube whether it is there, then load the matching page. */
    private void chooseView() {
        final String base = Prefs.baseUrl(this);
        new Thread(new Runnable() {
            @Override
            public void run() {
                final boolean home = Cube.reachable(base);
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (home) {
                            loadLive();
                        } else {
                            loadHistory();
                        }
                    }
                });
                if (home) {
                    Sync.run(getApplicationContext());
                }
            }
        }, "cubej1-probe").start();
    }

    private void loadLive() {
        offline = false;
        // The bridge belongs to the bundled page only; the Cube's page has
        // no business reaching local storage.
        web.removeJavascriptInterface(BRIDGE_NAME);
        loadFailed = false;
        web.loadUrl(Prefs.baseUrl(this) + "/");
    }

    private void loadHistory() {
        offline = true;
        web.addJavascriptInterface(bridge, BRIDGE_NAME);
        loadFailed = false;
        setup.setVisibility(View.GONE);
        web.loadUrl(HISTORY_PAGE);
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
            return false;
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
            if (offline) {
                return; // the asset page has nothing to fail against
            }
            loadFailed = true;
            // Falling back is better than an error screen: the phone still
            // holds everything up to the last visit.
            loadHistory();
            toast("Cube に届かないので履歴を表示します（" + e.getErrorCode() + "）");
        }

        @Override
        public void onPageFinished(WebView v, String u) {
            if (!loadFailed) {
                setup.setVisibility(View.GONE);
            }
        }
    }

    // -- menu --------------------------------------------------------------

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
        showMenu();
    }

    private void showMenu() {
        final String[] items = {
                "今すぐ同期",
                offline ? "ライブ表示を試す" : "履歴を見る",
                "CSV を書き出す（30分ごと）",
                "CSV を書き出す（分ごと）",
                "接続先を変更",
                "閉じる",
        };
        new AlertDialog.Builder(this)
                .setTitle("電力モニタ")
                .setItems(items, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        switch (which) {
                            case 0: syncNow(); break;
                            case 1: if (offline) { chooseView(); } else { loadHistory(); } break;
                            case 2: exportCsv(REQ_CSV_SLOT, "halfhour"); break;
                            case 3: exportCsv(REQ_CSV_MINUTE, "minute"); break;
                            case 4: showSetup("接続先の IP とポートを変更できます。"); break;
                            default: finish(); break;
                        }
                    }
                })
                .setNegativeButton("キャンセル", null)
                .show();
    }

    private void syncNow() {
        toast("同期しています...");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final Sync.Result r = Sync.run(getApplicationContext());
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        toast(r.describe());
                        if (r.outcome == Sync.Outcome.OK && offline) {
                            web.reload();
                        }
                    }
                });
            }
        }, "cubej1-sync-now").start();
    }

    // -- CSV ---------------------------------------------------------------

    /** Let the system picker choose where it goes: no storage permission. */
    private void exportCsv(int request, String kind) {
        String stamp = new SimpleDateFormat("yyyyMMdd", Locale.US).format(new Date());
        Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("text/csv");
        i.putExtra(Intent.EXTRA_TITLE, "cubej1-" + kind + "-" + stamp + ".csv");
        startActivityForResult(i, request);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        if (request != REQ_CSV_SLOT && request != REQ_CSV_MINUTE) {
            return;
        }
        final Uri uri = data.getData();
        final boolean slots = request == REQ_CSV_SLOT;
        toast("書き出しています...");
        new Thread(new Runnable() {
            @Override
            public void run() {
                String msg;
                try {
                    OutputStream os = getContentResolver().openOutputStream(uri);
                    if (os == null) {
                        throw new java.io.IOException("書き込み先を開けません");
                    }
                    int rows;
                    try {
                        rows = slots ? store.writeSlotCsv(os) : store.writeMinuteCsv(os);
                    } finally {
                        os.close();
                    }
                    msg = rows + " 行を書き出しました";
                } catch (Exception e) {
                    msg = "書き出しに失敗しました: " + e.getMessage();
                }
                final String done = msg;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        toast(done);
                    }
                });
            }
        }, "cubej1-csv").start();
    }

    private void toast(String m) {
        Toast.makeText(this, m, Toast.LENGTH_LONG).show();
    }

    // -- setup screen ------------------------------------------------------

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
            p = String.valueOf(Prefs.DEFAULT_PORT);
        }
        urlPreview.setText(h.isEmpty() ? "接続先: (未設定)" : "接続先: http://" + h + ":" + p + "/");
    }

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
                chooseView();
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

    private void showSetup(String message) {
        setupMessage.setText(message);
        hostField.setText(Prefs.host(this));
        portField.setText(String.valueOf(Prefs.port(this)));
        refreshPreview();
        setup.setVisibility(View.VISIBLE);
    }

    private void save() {
        String[] address = parseHost(hostField.getText().toString());
        int p = Prefs.DEFAULT_PORT;
        // A port pasted into the address field wins over the port box: it is
        // the more specific thing the person just typed.
        String raw = address[1] != null ? address[1] : portField.getText().toString().trim();
        if (!raw.isEmpty()) {
            try {
                int parsedPort = Integer.parseInt(raw);
                if (parsedPort >= 1 && parsedPort <= 65535) {
                    p = parsedPort;
                }
            } catch (NumberFormatException ignored) {
                // keep the default rather than refusing to move on
            }
        }
        Prefs.setAddress(this, address[0], p);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
