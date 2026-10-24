#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Can this Cube talk HTTPS?

Everything Google serves is HTTPS only, so whether the device can finish a
TLS handshake decides the whole shape of a cloud relay: the Cube posting
straight to Google, or something else on the LAN having to do it for it.
This asks the question and changes nothing.

Delivery (adb is already open on 5555):

    adb connect <CubeのIP>:5555
    adb push tls_probe.py /data/local/
    adb shell python /data/local/tls_probe.py

Reads nothing, writes nothing, opens only outbound connections.
Python 2.7 standard library only, same constraint as meter_hub.py.
"""

import socket
import sys
import time

HOSTS = ["script.google.com", "www.googleapis.com"]
TIMEOUT = 20


def line(k, v):
    print("%-26s %s" % (k, v))


def section(t):
    print("")
    print("-- %s %s" % (t, "-" * max(4, 50 - len(t))))


# -- 1. what is even available -------------------------------------------
section("environment")
line("python", sys.version.replace("\n", " "))
line("clock (UTC)", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
print("    ^ a wrong clock alone fails certificate verification")

try:
    import ssl
except ImportError as e:
    line("ssl module", "MISSING (%s)" % e)
    print("")
    print("=> No TLS on this device. A cloud relay cannot run on the Cube.")
    sys.exit(1)

line("ssl module", "present")
line("openssl", getattr(ssl, "OPENSSL_VERSION", "unknown"))
line("SSLContext", hasattr(ssl, "SSLContext"))
line("create_default_context", hasattr(ssl, "create_default_context"))
line("SNI (HAS_SNI)", getattr(ssl, "HAS_SNI", False))
line("TLSv1.2 constant", hasattr(ssl, "PROTOCOL_TLSv1_2"))

try:
    import httplib
    line("httplib", "present")
except ImportError:
    httplib = None
    line("httplib", "MISSING")


# -- 2. does a handshake complete at all ---------------------------------
def handshake(host, verify):
    """Return a one-line verdict. verify=False still sends SNI, which Google
    requires; without it the handshake fails for a reason unrelated to TLS."""
    raw = None
    try:
        raw = socket.create_connection((host, 443), timeout=TIMEOUT)
        if hasattr(ssl, "SSLContext"):
            if verify:
                ctx = ssl.create_default_context()
            else:
                ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            if getattr(ssl, "HAS_SNI", False):
                s = ctx.wrap_socket(raw, server_hostname=host)
            else:
                s = ctx.wrap_socket(raw)
        else:
            # Pre-2.7.9: no context, no SNI, no verification possible.
            s = ssl.wrap_socket(raw)
        proto = s.version() if hasattr(s, "version") else "?"
        cipher = s.cipher()[0] if s.cipher() else "?"
        s.close()
        return "OK   %s / %s" % (proto, cipher)
    except Exception as e:
        try:
            if raw is not None:
                raw.close()
        except Exception:
            pass
        return "FAIL %s: %s" % (type(e).__name__, e)


section("tls handshake")
for h in HOSTS:
    line(h + "  verify=off", handshake(h, False))
    line(h + "  verify=on", handshake(h, True))
print("    verify=off OK and verify=on FAIL means TLS works but the CA")
print("    bundle or the clock does not. That is still usable.")


# -- 3. a real request, the way an uploader would make it ----------------
section("https request")
if httplib is None:
    line("httplib GET", "skipped (module missing)")
else:
    for verify in (True, False):
        host = "www.googleapis.com"
        try:
            kwargs = {"timeout": TIMEOUT}
            if hasattr(ssl, "SSLContext"):
                if verify:
                    kwargs["context"] = ssl.create_default_context()
                else:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    kwargs["context"] = ctx
            c = httplib.HTTPSConnection(host, 443, **kwargs)
            c.request("GET", "/discovery/v1/apis?fields=kind")
            r = c.getresponse()
            body = r.read(120)
            c.close()
            line("GET verify=%s" % ("on" if verify else "off"),
                 "OK   %s %s  %r" % (r.status, r.reason, body[:60]))
        except Exception as e:
            line("GET verify=%s" % ("on" if verify else "off"),
                 "FAIL %s: %s" % (type(e).__name__, e))

section("verdict")
print("Read it like this:")
print("  handshake verify=off OK   -> the Cube can reach Google itself.")
print("                               An uploader inside meter_hub.py is viable.")
print("  everything FAIL           -> no TLS here. The relay has to live")
print("                               somewhere else on the LAN.")
