package net.nekomami.cubej1;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * The Cube's HTTP API, as seen from the phone.
 *
 * Reachability is tested by asking the Cube rather than by reading the
 * Wi-Fi SSID: naming the network would need a location permission, and
 * answering /healthz is the stronger fact anyway — it means the meter_hub
 * process is alive, not merely that some Wi-Fi is attached.
 */
final class Cube {

    /** Short on purpose: away from home this fails on every attempt. */
    private static final int PROBE_TIMEOUT_MS = 2500;
    /**
     * Used when someone asked to go to the live view and is watching for it.
     * The short probe is right for startup, where failing is the expected
     * answer, but wrong here: a phone that has just joined the Wi-Fi, or a
     * Cube busy answering a history request, can take several seconds to
     * reply, and giving up at 2.5s reports it as unreachable when it is not.
     */
    private static final int ASK_TIMEOUT_MS = 9000;
    private static final int READ_TIMEOUT_MS = 20000;
    private static final int MAX_BODY = 8 * 1024 * 1024;

    private Cube() { }

    static boolean reachable(String base) {
        return reachable(base, PROBE_TIMEOUT_MS);
    }

    /** Waits longer, and tries twice, for a check someone is waiting on. */
    static boolean reachableAsked(String base) {
        return reachable(base, ASK_TIMEOUT_MS) || reachable(base, ASK_TIMEOUT_MS);
    }

    static boolean reachable(String base, int timeoutMs) {
        try {
            return "ok".equals(get(base, "/healthz", timeoutMs).trim());
        } catch (Exception e) {
            return false;
        }
    }

    static String get(String base, String path) throws IOException {
        return get(base, path, READ_TIMEOUT_MS);
    }

    static String get(String base, String path, int timeoutMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(base + path).openConnection();
        c.setConnectTimeout(timeoutMs);
        c.setReadTimeout(timeoutMs);
        c.setRequestMethod("GET");
        c.setUseCaches(false);
        try {
            int status = c.getResponseCode();
            if (status != 200) {
                throw new IOException("HTTP " + status + " for " + path);
            }
            InputStream in = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                if (out.size() > MAX_BODY) {
                    throw new IOException("response too large for " + path);
                }
            }
            return out.toString("UTF-8");
        } finally {
            c.disconnect();
        }
    }
}
