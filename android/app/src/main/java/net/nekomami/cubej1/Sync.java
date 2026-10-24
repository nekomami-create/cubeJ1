package net.nekomami.cubej1;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * One pass of pulling the Cube's history onto the phone.
 *
 * Safe to run from anywhere and at any time: when the Cube is not reachable
 * it gives up in a couple of seconds, and every write is keyed by timestamp
 * so overlapping fetches cost nothing but a little bandwidth.
 */
final class Sync {

    /** The Cube keeps 400 days of half-hour data; take all of it once. */
    private static final int BACKFILL_DAYS = 400;
    /** After that, only what can have changed since the last visit. */
    private static final int CATCHUP_DAYS = 10;

    enum Outcome { OK, NOT_CONFIGURED, UNREACHABLE, FAILED }

    static final class Result {
        final Outcome outcome;
        final int minutes;
        final int days;
        final String error;

        Result(Outcome o, int minutes, int days, String error) {
            this.outcome = o;
            this.minutes = minutes;
            this.days = days;
            this.error = error;
        }

        String describe() {
            switch (outcome) {
                case OK:
                    return "同期しました（" + days + "日分 / " + minutes + "件）";
                case NOT_CONFIGURED:
                    return "接続先が未設定です";
                case UNREACHABLE:
                    return "Cube に届きません（家の Wi-Fi にいないか、Cube が落ちています）";
                default:
                    return "同期に失敗しました: " + error;
            }
        }
    }

    /** Serialises the job and a tap on "今すぐ同期". */
    private static final Object LOCK = new Object();

    private Sync() { }

    static Result run(Context ctx) {
        synchronized (LOCK) {
            return runLocked(ctx);
        }
    }

    private static Result runLocked(Context ctx) {
        if (!Prefs.configured(ctx)) {
            return new Result(Outcome.NOT_CONFIGURED, 0, 0, null);
        }
        String base = Prefs.baseUrl(ctx);
        if (!Cube.reachable(base)) {
            return new Result(Outcome.UNREACHABLE, 0, 0, null);
        }

        Store store = Store.get(ctx);
        try {
            // Per-minute detail. The Cube holds 24h of it, so anything older
            // than the last visit is already gone and cannot be recovered.
            int minutes = 0;
            JSONArray samples = new JSONObject(Cube.get(base, "/api/history"))
                    .optJSONArray("samples");
            if (samples != null) {
                minutes = store.putMinutes(samples);
            }

            // Half-hour record. Ask for everything the first time, then only
            // the recent days: earlier ones cannot change.
            int want = store.dayCount() == 0 ? BACKFILL_DAYS : CATCHUP_DAYS;
            int days = 0;
            JSONArray fetched = new JSONObject(Cube.get(base, "/api/daily?days=" + want))
                    .optJSONArray("days");
            if (fetched != null) {
                days = store.putDays(fetched);
            }

            store.pruneMinutes();
            Prefs.setLastSync(ctx, System.currentTimeMillis());
            return new Result(Outcome.OK, minutes, days, null);
        } catch (Exception e) {
            return new Result(Outcome.FAILED, 0, 0, e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }
}
