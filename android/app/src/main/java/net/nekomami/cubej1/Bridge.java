package net.nekomami.cubej1;

import android.content.Context;
import android.webkit.JavascriptInterface;

import org.json.JSONObject;

/**
 * What the offline history page is allowed to ask for.
 *
 * Read-only and local: it hands back rows already on this phone and can
 * neither write to the database nor touch the filesystem. It is attached
 * only while the bundled asset page is loaded, never while the WebView is
 * showing the Cube's own page.
 */
final class Bridge {

    private final Context ctx;
    private final Store store;

    Bridge(Context ctx, Store store) {
        this.ctx = ctx;
        this.store = store;
    }

    @JavascriptInterface
    public String days(int limit) {
        try {
            return store.daysJson(Math.max(1, Math.min(limit, 500)));
        } catch (Exception e) {
            return "{\"days\":[]}";
        }
    }

    @JavascriptInterface
    public String day(String date) {
        try {
            return store.dayJson(date);
        } catch (Exception e) {
            return "{}";
        }
    }

    @JavascriptInterface
    public String minutes(double fromTs, double toTs) {
        try {
            return store.minutesJson((long) fromTs, (long) toTs);
        } catch (Exception e) {
            return "{\"samples\":[]}";
        }
    }

    @JavascriptInterface
    public String status() {
        try {
            JSONObject o = new JSONObject();
            o.put("last_sync", Prefs.lastSync(ctx));
            o.put("now", System.currentTimeMillis());
            o.put("days", store.dayCount());
            o.put("minutes", store.minuteCount());
            o.put("newest_minute", store.newestMinuteTs());
            o.put("host", Prefs.host(ctx));
            return o.toString();
        } catch (Exception e) {
            return "{}";
        }
    }
}
