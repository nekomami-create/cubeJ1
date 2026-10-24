package net.nekomami.cubej1;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.util.Locale;

/**
 * The phone's copy of the meter's history.
 *
 * The Cube keeps half-hour data for 400 days on disk but only 24 hours of
 * per-minute samples in memory, so the two are stored with different
 * expectations: slots are the long record and are never pruned here, while
 * minutes are a recent detail view and age out.
 */
final class Store extends SQLiteOpenHelper {

    private static final String DB = "meter.db";
    private static final int VERSION = 1;

    /** How long per-minute samples are kept on the phone. */
    static final int MINUTE_KEEP_DAYS = 90;

    private static Store instance;

    /**
     * One instance for the process. The periodic job and the UI can both
     * want the database at the same moment, and two SQLiteOpenHelper
     * instances mean two connections and a chance of a locked write.
     */
    static synchronized Store get(Context c) {
        if (instance == null) {
            instance = new Store(c.getApplicationContext());
        }
        return instance;
    }

    private Store(Context c) {
        super(c, DB, null, VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE minute ("
                + "ts INTEGER PRIMARY KEY,"
                + "power_w REAL, current_r REAL, current_t REAL)");
        // One row per half-hour slot. use_kwh is consumption from this slot
        // to the next, so a day's last slot stays null until the next day's
        // first reading arrives; re-syncing fills it in.
        db.execSQL("CREATE TABLE slot ("
                + "date TEXT NOT NULL, slot INTEGER NOT NULL,"
                + "use_kwh REAL, cum_kwh REAL,"
                + "PRIMARY KEY (date, slot))");
        db.execSQL("CREATE TABLE day ("
                + "date TEXT PRIMARY KEY, total_kwh REAL, complete INTEGER)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int from, int to) {
        // Nothing to migrate yet. The data is a cache of the Cube's own
        // record, so dropping and re-syncing is always an option.
    }

    // -- writing -----------------------------------------------------------

    /** @return how many samples were new or changed. */
    int putMinutes(JSONArray samples) throws JSONException {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        int n = 0;
        try {
            for (int i = 0; i < samples.length(); i++) {
                JSONArray s = samples.getJSONArray(i);
                if (s.length() < 1 || s.isNull(0)) {
                    continue;
                }
                ContentValues v = new ContentValues();
                v.put("ts", s.getLong(0));
                putNullableDouble(v, "power_w", s, 1);
                putNullableDouble(v, "current_r", s, 2);
                putNullableDouble(v, "current_t", s, 3);
                db.insertWithOnConflict("minute", null, v, SQLiteDatabase.CONFLICT_REPLACE);
                n++;
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
        return n;
    }

    /** @return how many days were stored. */
    int putDays(JSONArray days) throws JSONException {
        SQLiteDatabase db = getWritableDatabase();
        db.beginTransaction();
        int n = 0;
        try {
            for (int i = 0; i < days.length(); i++) {
                JSONObject d = days.getJSONObject(i);
                String date = d.optString("date", "");
                if (date.isEmpty()) {
                    continue;
                }
                ContentValues dv = new ContentValues();
                dv.put("date", date);
                if (!d.isNull("total_kwh")) {
                    dv.put("total_kwh", d.getDouble("total_kwh"));
                }
                dv.put("complete", d.optBoolean("complete", false) ? 1 : 0);
                db.insertWithOnConflict("day", null, dv, SQLiteDatabase.CONFLICT_REPLACE);

                JSONArray use = d.optJSONArray("use_kwh");
                JSONArray cum = d.optJSONArray("cum_kwh");
                for (int s = 0; s < 48; s++) {
                    ContentValues sv = new ContentValues();
                    sv.put("date", date);
                    sv.put("slot", s);
                    if (use != null && s < use.length() && !use.isNull(s)) {
                        sv.put("use_kwh", use.getDouble(s));
                    }
                    if (cum != null && s < cum.length() && !cum.isNull(s)) {
                        sv.put("cum_kwh", cum.getDouble(s));
                    }
                    db.insertWithOnConflict("slot", null, sv, SQLiteDatabase.CONFLICT_REPLACE);
                }
                n++;
            }
            db.setTransactionSuccessful();
        } finally {
            db.endTransaction();
        }
        return n;
    }

    private static void putNullableDouble(ContentValues v, String col, JSONArray a, int idx) {
        if (idx < a.length() && !a.isNull(idx)) {
            v.put(col, a.optDouble(idx));
        }
    }

    void pruneMinutes() {
        long cutoff = System.currentTimeMillis() / 1000L - (long) MINUTE_KEEP_DAYS * 86400L;
        getWritableDatabase().delete("minute", "ts < ?", new String[]{String.valueOf(cutoff)});
    }

    // -- reading -----------------------------------------------------------

    int dayCount() {
        Cursor c = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM day", null);
        try {
            return c.moveToFirst() ? c.getInt(0) : 0;
        } finally {
            c.close();
        }
    }

    int minuteCount() {
        Cursor c = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM minute", null);
        try {
            return c.moveToFirst() ? c.getInt(0) : 0;
        } finally {
            c.close();
        }
    }

    /** Newest first, for the day list. */
    String daysJson(int limit) throws JSONException {
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT date, total_kwh, complete FROM day ORDER BY date DESC LIMIT ?",
                new String[]{String.valueOf(limit)});
        JSONArray out = new JSONArray();
        try {
            while (c.moveToNext()) {
                JSONObject o = new JSONObject();
                o.put("date", c.getString(0));
                o.put("total_kwh", c.isNull(1) ? JSONObject.NULL : c.getDouble(1));
                o.put("complete", c.getInt(2) == 1);
                out.put(o);
            }
        } finally {
            c.close();
        }
        return new JSONObject().put("days", out).toString();
    }

    /** The 48 slots of one day, in order. */
    String dayJson(String date) throws JSONException {
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT slot, use_kwh, cum_kwh FROM slot WHERE date = ? ORDER BY slot",
                new String[]{date});
        JSONArray use = new JSONArray();
        JSONArray cum = new JSONArray();
        try {
            while (c.moveToNext()) {
                use.put(c.isNull(1) ? JSONObject.NULL : c.getDouble(1));
                cum.put(c.isNull(2) ? JSONObject.NULL : c.getDouble(2));
            }
        } finally {
            c.close();
        }
        JSONObject o = new JSONObject();
        o.put("date", date);
        o.put("use_kwh", use);
        o.put("cum_kwh", cum);
        return o.toString();
    }

    /** Per-minute samples between two epoch seconds, oldest first. */
    String minutesJson(long fromTs, long toTs) throws JSONException {
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT ts, power_w, current_r, current_t FROM minute "
                        + "WHERE ts BETWEEN ? AND ? ORDER BY ts",
                new String[]{String.valueOf(fromTs), String.valueOf(toTs)});
        JSONArray out = new JSONArray();
        try {
            while (c.moveToNext()) {
                JSONArray s = new JSONArray();
                s.put(c.getLong(0));
                s.put(c.isNull(1) ? JSONObject.NULL : c.getDouble(1));
                s.put(c.isNull(2) ? JSONObject.NULL : c.getDouble(2));
                s.put(c.isNull(3) ? JSONObject.NULL : c.getDouble(3));
                out.put(s);
            }
        } finally {
            c.close();
        }
        return new JSONObject().put("samples", out).toString();
    }

    long newestMinuteTs() {
        Cursor c = getReadableDatabase().rawQuery("SELECT MAX(ts) FROM minute", null);
        try {
            return c.moveToFirst() && !c.isNull(0) ? c.getLong(0) : 0L;
        } finally {
            c.close();
        }
    }

    // -- export ------------------------------------------------------------

    /** Half-hour usage, one row per slot. @return rows written. */
    int writeSlotCsv(OutputStream os) throws IOException {
        Writer w = new OutputStreamWriter(os, "UTF-8");
        w.write("date,slot,time,use_kwh,cum_kwh\n");
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT date, slot, use_kwh, cum_kwh FROM slot ORDER BY date, slot", null);
        int rows = 0;
        try {
            while (c.moveToNext()) {
                int slot = c.getInt(1);
                w.write(String.format(Locale.US, "%s,%d,%02d:%02d,%s,%s\n",
                        c.getString(0), slot, slot / 2, (slot % 2) * 30,
                        c.isNull(2) ? "" : String.format(Locale.US, "%.4f", c.getDouble(2)),
                        c.isNull(3) ? "" : String.format(Locale.US, "%.1f", c.getDouble(3))));
                rows++;
            }
        } finally {
            c.close();
            w.flush();
        }
        return rows;
    }

    /** Per-minute samples, one row each. @return rows written. */
    int writeMinuteCsv(OutputStream os) throws IOException {
        Writer w = new OutputStreamWriter(os, "UTF-8");
        w.write("epoch,iso_local,power_w,current_r_a,current_t_a\n");
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT ts, power_w, current_r, current_t FROM minute ORDER BY ts", null);
        int rows = 0;
        try {
            while (c.moveToNext()) {
                long ts = c.getLong(0);
                w.write(String.format(Locale.US, "%d,%s,%s,%s,%s\n",
                        ts,
                        android.text.format.DateFormat.format("yyyy-MM-dd HH:mm:ss", ts * 1000L),
                        c.isNull(1) ? "" : String.format(Locale.US, "%.0f", c.getDouble(1)),
                        c.isNull(2) ? "" : String.format(Locale.US, "%.1f", c.getDouble(2)),
                        c.isNull(3) ? "" : String.format(Locale.US, "%.1f", c.getDouble(3))));
                rows++;
            }
        } finally {
            c.close();
            w.flush();
        }
        return rows;
    }
}
