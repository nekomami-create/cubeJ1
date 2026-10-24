package net.nekomami.cubej1;

import android.os.Handler;
import android.os.Looper;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Finds the Cube on the local network, the way find-cube.ps1 does on a PC.
 *
 * Needed because the address is the one thing the app cannot work without
 * and the one thing DHCP is free to change. Reading the phone's own address
 * off a network interface needs no permission, unlike asking which Wi-Fi
 * this is.
 */
final class Scanner {

    /** Only the first 254 addresses of a /24, same as the PowerShell one. */
    private static final int CONNECT_TIMEOUT_MS = 350;
    private static final int THREADS = 32;
    private static final int OVERALL_TIMEOUT_S = 25;

    interface Listener {
        /** Always called on the main thread. */
        void onDone(List<String> found, String subnet, String error);
    }

    private Scanner() { }

    /** The phone's own IPv4 on a real interface, or null. */
    static String localIPv4() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()
                            && a.isSiteLocalAddress()) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {
            // fall through to the null answer; the caller explains it
        }
        return null;
    }

    static void scan(final int port, final Listener listener) {
        final Handler ui = new Handler(Looper.getMainLooper());
        new Thread(new Runnable() {
            @Override
            public void run() {
                String me = localIPv4();
                if (me == null) {
                    post(ui, listener, null, null, "この端末の IPv4 アドレスが分かりません。Wi-Fi に繋がっていますか。");
                    return;
                }
                String subnet = me.substring(0, me.lastIndexOf('.'));
                post(ui, listener, sweep(subnet, port), subnet, null);
            }
        }, "cubej1-scan").start();
    }

    /**
     * The search itself, with nothing from the Android side in it so it can
     * be exercised directly. Blocks until done.
     */
    static List<String> sweep(final String subnet, final int port) {
        // A plain TCP connect first: most of a subnet is empty and refuses
        // instantly, so this costs far less than an HTTP request to every
        // address.
        final List<String> open = Collections.synchronizedList(new ArrayList<String>());
        ExecutorService pool = Executors.newFixedThreadPool(THREADS);
        for (int i = 1; i <= 254; i++) {
            final String ip = subnet + "." + i;
            pool.execute(new Runnable() {
                @Override
                public void run() {
                    Socket s = new Socket();
                    try {
                        s.connect(new InetSocketAddress(ip, port), CONNECT_TIMEOUT_MS);
                        open.add(ip);
                    } catch (Exception ignored) {
                        // nothing listening; the common case
                    } finally {
                        try {
                            s.close();
                        } catch (Exception ignored2) {
                            // already gone
                        }
                    }
                }
            });
        }
        pool.shutdown();
        try {
            pool.awaitTermination(OVERALL_TIMEOUT_S, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }

        // Something else may well answer on this port, so confirm it is the
        // hub and not merely an open socket.
        List<String> found = new ArrayList<String>();
        for (String ip : new ArrayList<String>(open)) {
            if (Cube.reachable("http://" + ip + ":" + port)) {
                found.add(ip);
            }
        }
        Collections.sort(found);
        return found;
    }

    private static void post(Handler ui, final Listener l, final List<String> found,
                             final String subnet, final String error) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                l.onDone(found, subnet, error);
            }
        });
    }
}
