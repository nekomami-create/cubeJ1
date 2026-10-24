package net.nekomami.cubej1;

import android.content.Context;
import android.content.SharedPreferences;

/** Where the Cube is, and when it was last reached. */
final class Prefs {

    static final int DEFAULT_PORT = 8080;

    private static final String NAME = "cubej1";
    private static final String HOST = "host";
    private static final String PORT = "port";
    private static final String LAST_SYNC = "last_sync";

    private Prefs() { }

    private static SharedPreferences of(Context c) {
        return c.getSharedPreferences(NAME, Context.MODE_PRIVATE);
    }

    static String host(Context c) {
        return of(c).getString(HOST, "").trim();
    }

    static int port(Context c) {
        return of(c).getInt(PORT, DEFAULT_PORT);
    }

    static boolean configured(Context c) {
        return !host(c).isEmpty();
    }

    static void setAddress(Context c, String host, int port) {
        of(c).edit().putString(HOST, host).putInt(PORT, port).apply();
    }

    /** Epoch millis of the last sync that stored something, or 0. */
    static long lastSync(Context c) {
        return of(c).getLong(LAST_SYNC, 0L);
    }

    static void setLastSync(Context c, long millis) {
        of(c).edit().putLong(LAST_SYNC, millis).apply();
    }

    static String baseUrl(Context c) {
        return "http://" + host(c) + ":" + port(c);
    }
}
