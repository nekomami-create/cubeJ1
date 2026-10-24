package net.nekomami.cubej1;

import android.content.Context;
import android.webkit.JavascriptInterface;

import org.json.JSONObject;

/**
 * What the offline history page is allowed to ask for.
 *
 * Read-only and local: it hands back rows already on this phone and can
 * neither write to the database nor touch the filesystem. The one thing it
 * can start is a CSV export, and that only opens the system's save dialog;
 * where the file goes is chosen by the person, not the page. It is attached
 * only while the bundled asset page is loaded, never while the WebView is
 * showing the Cube's own page.
 */
final class Bridge {

    /** Opens the save dialog. Called on a WebView thread, not the UI thread. */
    interface Exporter {
        void exportCsv(String kind, String month);
    }

    private final Context ctx;
    private final Store store;
    private final Exporter exporter;

    Bridge(Context ctx, Store store, Exporter exporter) {
        this.ctx = ctx;
        this.store = store;
        this.exporter = exporter;
    }

    @JavascriptInterface
    public String months() {
        try {
            return store.monthsJson();
        } catch (Exception e) {
            return "{\"months\":[]}";
        }
    }

    @JavascriptInterface
    public String daysInMonth(String month) {
        try {
            return store.daysInMonthJson(month);
        } catch (Exception e) {
            return "{\"days\":[]}";
        }
    }

    /**
     * @param kind  "halfhour" or "minute"
     * @param month "2026-10" for that month, empty for everything
     */
    @JavascriptInterface
    public void exportCsv(String kind, String month) {
        if (!"halfhour".equals(kind) && !"minute".equals(kind)) {
            return;
        }
        exporter.exportCsv(kind, Store.isMonth(month) ? month : null);
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
    public String minutesForDate(String date) {
        try {
            return store.minutesForDateJson(date);
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
