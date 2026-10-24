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
    private static final int READ_TIMEOUT_MS = 20000;
    private static final int MAX_BODY = 8 * 1024 * 1024;

    private Cube() { }

    static boolean reachable(String base) {
        try {
            return "ok".equals(get(base, "/healthz", PROBE_TIMEOUT_MS).trim());
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
