#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
meter_hub.py - Cube J1 local hub (no Home Assistant, no external broker)

The bridge (this repository's fork of upstream mqtt_bridge.py) is pointed
at 127.0.0.1 and publishes there. This process:

  1. speaks just enough MQTT 3.1.1 server-side to receive those publishes
  2. keeps the latest reading, ~24h of per-minute samples, and the meter's
     half-hour cumulative readings per day, saved under /data/local
  3. serves a phone-friendly page and a JSON API over plain HTTP

Deliberate constraints, because the Cube has Python 2.7 and no package
manager: only modules the bridge itself already proves are present are
used (os, sys, json, time, socket, struct, threading, collections).
No BaseHTTPServer, no SocketServer, no third-party packages.

Endpoints:
    GET /              phone dashboard (HTML)
    GET /api           latest reading (JSON)
    GET /api/history   ring buffer of recent samples, ?since=TS (JSON)
    GET /api/tariff    electricity price table for the estimate (JSON)
    GET /api/daily     half-hour history per day, ?days=N (JSON)
    GET /healthz       "ok"
"""

import os
import sys
import json
import time
import socket
import struct
import threading
import collections

CONFIG_PATH = "/data/local/config.json"
LOG_PATH    = "/data/local/meter_hub.log"
BRIDGE_LOG  = "/data/local/mqtt_bridge.log"

# The bridge appends to its log forever with no rotation. Keep both logs
# bounded so /data cannot slowly fill up over a few years of uptime.
LOG_MAX     = 1024 * 1024
LOG_KEEP    = 256 * 1024
JANITOR_SEC = 300

DEFAULT_HUB_PORT   = 11883   # not 1883: avoid clashing with anything preinstalled
DEFAULT_HTTP_PORT  = 8080    # not 80/443: nginx may already hold those
DEFAULT_HISTORY    = 1440    # 24h at one sample per minute

# Half-hour history from the meter. It only holds about nine days itself, so
# the Cube keeps what it has seen and the record grows past that window.
DAILY_PATH    = "/data/local/meter_daily.json"
DAILY_KEEP    = 400          # days
RING_PATH     = "/data/local/meter_ring.json"
RING_SAVE_SEC = 600          # per-minute samples survive a restart, 10 min stale at most

# Electricity price, used only by the dashboard to show an estimate.
# Built in: TEPCO 電化上手 at 7-10kVA, checked on tepco.co.jp on 2026-09-13.
# Every price includes tax. fuel_yen_kwh is keyed by billing month ("N月分",
# the month the meter-reading day falls in) and already contains the
# government discount. The fuel adjustment changes every month, so a
# tariff.json here can add months or override any key without a restart.
TARIFF_PATH = "/data/local/tariff.json"
DEFAULT_TARIFF = {
    "name": "電化上手（7〜10kVA）",
    "checked": "2026-09-13",
    "meter_day": 22,
    "basic_yen": 2457.5,
    "hours": ["night"] * 7 + ["me"] * 3 + ["day"] * 7 + ["me"] * 6 + ["night"],
    "band_order": ["day", "me", "night"],
    "labels": {"day": "昼間", "me": "朝晩", "night": "夜間"},
    "rates": {"day": 40.44, "me": 35.87, "night": 28.85},
    "summer_months": [7, 8, 9],
    "summer_rates": {"day": 43.93},
    "renewable_yen_kwh": 4.18,
    "fuel_yen_kwh": {"2026-09": -10.96, "2026-10": -9.30},
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_file = None

def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if _log_file:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass
    else:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class State(object):
    def __init__(self, maxlen):
        self.lock    = threading.Lock()
        self.latest  = {}
        self.history = collections.deque(maxlen=maxlen)
        self.started = time.time()
        self.publishes = 0
        self.days    = {}      # "YYYY-MM-DD" -> 48 cumulative kWh (or None)
        self.days_dirty = False

    def update(self, key, value):
        with self.lock:
            self.latest[key] = value
            self.latest["ts"] = int(time.time())
            self.publishes += 1

    def snapshot(self):
        """Append one point to the ring buffer. Called when power arrives."""
        with self.lock:
            self.history.append((
                int(time.time()),
                self.latest.get("power_w"),
                self.latest.get("current_r_a"),
                self.latest.get("current_t_a"),
            ))

    def get_latest(self):
        with self.lock:
            d = dict(self.latest)
            d["samples"] = len(self.history)
            d["publishes"] = self.publishes
        d["uptime_s"] = int(time.time() - self.started)
        d["now"] = int(time.time())
        return d

    def get_history(self):
        with self.lock:
            return list(self.history)

    def set_day(self, date, kwh):
        with self.lock:
            old = self.days.get(date)
            if old is not None:
                # Keep any slot already known: a later fetch never has less.
                kwh = [n if n is not None else o for n, o in zip(kwh, old)]
            self.days[date] = kwh
            for d in sorted(self.days)[:-DAILY_KEEP]:
                del self.days[d]
            self.days_dirty = True

def next_date(d):
    """'YYYY-MM-DD' -> following day. Noon keeps the arithmetic clear of any
    timezone offset, whichever the process runs under."""
    y, m, dd = [int(x) for x in d.split("-")]
    t = time.mktime((y, m, dd, 12, 0, 0, 0, 0, -1)) + 86400
    return time.strftime("%Y-%m-%d", time.localtime(t))

def daily_payload(state, limit):
    """Per-day half-hour usage. use_kwh[i] is consumption from slot i to i+1;
    the last slot of a day needs the next day's first reading."""
    with state.lock:
        days = dict(state.days)
    out = []
    for d in sorted(days)[-limit:]:
        cum = days[d]
        nxt = days.get(next_date(d))
        use = []
        for i in range(48):
            a = cum[i]
            b = cum[i + 1] if i < 47 else (nxt[0] if nxt else None)
            if a is None or b is None or b < a:
                use.append(None)
            else:
                use.append(round(b - a, 4))
        known = [u for u in use if u is not None]
        out.append({
            "date": d,
            "cum_kwh": cum,
            "use_kwh": use,
            "total_kwh": round(sum(known), 3) if known else None,
            "complete": len(known) == 48,
        })
    return {"days": out}

# Topic leaf name -> (key in the JSON API, converter)
TOPIC_MAP = {
    "power":          ("power_w",            float),
    "energy_forward": ("energy_forward_kwh", float),
    "energy_reverse": ("energy_reverse_kwh", float),
    "current_r":      ("current_r_a",        float),
    "current_t":      ("current_t_a",        float),
}

def on_publish(state, topic, payload):
    # Home Assistant auto-discovery messages are irrelevant here.
    if topic.startswith("homeassistant/"):
        return
    if topic.endswith("/history_day"):
        try:
            msg = json.loads(payload)
            date = str(msg["date"])
            kwh = msg["kwh"]
            if len(date) != 10 or not isinstance(kwh, list) or len(kwh) != 48:
                raise ValueError("unexpected shape")
            state.set_day(date, [None if v is None else float(v) for v in kwh])
        except Exception as e:
            log("history_day rejected: %s" % e)
        return
    leaf = topic.rsplit("/", 1)[-1]
    entry = TOPIC_MAP.get(leaf)
    if not entry:
        return
    key, cast = entry
    try:
        value = cast(payload.strip())
    except Exception:
        return
    state.update(key, value)
    # power is published once per poll cycle, so use it as the sample clock.
    if leaf == "power":
        state.snapshot()

# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 server side
#
# Only what the upstream client actually sends: CONNECT, PUBLISH (QoS 0),
# PINGREQ, DISCONNECT. It reads the socket only for CONNACK and PINGRESP,
# so nothing unsolicited must ever be written back to it.
# ---------------------------------------------------------------------------

class PacketReader(object):
    def __init__(self, sock):
        self.sock = sock
        self.buf  = b""

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise EOFError("peer closed")
            self.buf += chunk
        out = self.buf[:n]
        self.buf = self.buf[n:]
        return out

    def byte(self):
        return ord(self.read(1))

def read_remaining_length(reader):
    mult  = 1
    value = 0
    for _ in range(4):
        b = reader.byte()
        value += (b & 0x7F) * mult
        if not (b & 0x80):
            return value
        mult *= 128
    raise ValueError("malformed remaining length")

def handle_mqtt_client(conn, addr, state):
    conn.settimeout(900)
    reader = PacketReader(conn)
    peer = addr[0]
    try:
        while True:
            b0 = reader.byte()
            remaining = read_remaining_length(reader)
            body = reader.read(remaining) if remaining else b""
            ptype = b0 >> 4

            if ptype == 1:      # CONNECT
                # CONNACK: session present = 0, return code = 0 (accepted)
                conn.sendall(b"\x20\x02\x00\x00")
                log("mqtt: client connected from %s" % peer)

            elif ptype == 3:    # PUBLISH
                qos = (b0 >> 1) & 0x03
                if len(body) < 2:
                    continue
                tlen = struct.unpack(">H", body[0:2])[0]
                topic = body[2:2 + tlen].decode("utf-8", "replace")
                idx = 2 + tlen
                packet_id = None
                if qos > 0:
                    packet_id = struct.unpack(">H", body[idx:idx + 2])[0]
                    idx += 2
                on_publish(state, topic, body[idx:])
                if qos == 1 and packet_id is not None:
                    conn.sendall(b"\x40\x02" + struct.pack(">H", packet_id))

            elif ptype == 8:    # SUBSCRIBE (unused, but answer politely)
                if len(body) >= 2:
                    packet_id = struct.unpack(">H", body[0:2])[0]
                    conn.sendall(b"\x90\x03" + struct.pack(">H", packet_id) + b"\x00")

            elif ptype == 12:   # PINGREQ
                conn.sendall(b"\xD0\x00")

            elif ptype == 14:   # DISCONNECT
                log("mqtt: client %s disconnected" % peer)
                break

            # anything else is ignored on purpose

    except EOFError:
        log("mqtt: client %s closed the connection" % peer)
    except Exception as e:
        log("mqtt: client %s ended: %s" % (peer, e))
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Minimal HTTP server
# ---------------------------------------------------------------------------

def _to_bytes(s):
    """py2: str is already bytes and passes through; unicode gets encoded.
    Keeps Content-Length in BYTES, which matters because the page is UTF-8
    Japanese: a character count here would truncate it in the browser."""
    if isinstance(s, bytes):
        return s
    return s.encode("utf-8")

def http_response(status, ctype, body):
    body = _to_bytes(body)
    head = ("HTTP/1.1 %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Cache-Control: no-store\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n"
            "\r\n") % (status, ctype, len(body))
    return _to_bytes(head) + body

def handle_http_client(conn, addr, state):
    conn.settimeout(15)
    try:
        data = ""
        while "\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 16384:
                conn.sendall(http_response("431 Request Header Fields Too Large",
                                           "text/plain", "too large"))
                return

        request_line = data.split("\r\n", 1)[0]
        parts = request_line.split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split("?", 1)[0]

        if method not in ("GET", "HEAD"):
            conn.sendall(http_response("405 Method Not Allowed", "text/plain", "GET only"))
            return

        if path in ("/", "/index.html"):
            conn.sendall(http_response("200 OK", "text/html; charset=utf-8", PAGE))
        elif path in ("/api", "/api/"):
            body = json.dumps(state.get_latest(), separators=(",", ":"))
            conn.sendall(http_response("200 OK", "application/json; charset=utf-8", body))
        elif path in ("/api/history", "/api/history/"):
            # ?since=TS returns only newer samples, so a page on a lossy link
            # need not pull the whole day again every minute.
            since = 0
            query = parts[1].split("?", 1)
            if len(query) == 2:
                for kv in query[1].split("&"):
                    if kv.startswith("since="):
                        try:
                            since = int(float(kv[6:]))
                        except ValueError:
                            pass
            samples = [list(s) for s in state.get_history() if s[0] > since]
            body = json.dumps({"samples": samples}, separators=(",", ":"))
            conn.sendall(http_response("200 OK", "application/json; charset=utf-8", body))
        elif path in ("/api/daily", "/api/daily/"):
            limit = DAILY_KEEP
            query = parts[1].split("?", 1)
            if len(query) == 2:
                for kv in query[1].split("&"):
                    if kv.startswith("days="):
                        try:
                            limit = max(1, min(DAILY_KEEP, int(kv[5:])))
                        except ValueError:
                            pass
            body = json.dumps(daily_payload(state, limit), separators=(",", ":"))
            conn.sendall(http_response("200 OK", "application/json; charset=utf-8", body))
        elif path in ("/api/tariff", "/api/tariff/"):
            # Read on every request so an edited tariff.json needs no restart.
            body = json.dumps(load_tariff(), separators=(",", ":"))
            conn.sendall(http_response("200 OK", "application/json; charset=utf-8", body))
        elif path == "/favicon.svg":
            conn.sendall(http_response("200 OK", "image/svg+xml", FAVICON))
        elif path == "/favicon.ico":
            conn.sendall(http_response("204 No Content", "image/x-icon", ""))
        elif path == "/healthz":
            conn.sendall(http_response("200 OK", "text/plain", "ok"))
        else:
            conn.sendall(http_response("404 Not Found", "text/plain", "not found"))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Housekeeping: keep both log files bounded
# ---------------------------------------------------------------------------

def trim_log(path, max_bytes, keep_bytes):
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) <= max_bytes:
            return
        f = open(path, "rb")
        try:
            f.seek(-keep_bytes, os.SEEK_END)
            tail = f.read()
        finally:
            f.close()
        cut = tail.find(b"\n")
        if cut >= 0:
            tail = tail[cut + 1:]
        # Truncate in place. The bridge holds this file open with O_APPEND,
        # so its later writes still land at the (new) end of the same inode.
        g = open(path, "wb")
        try:
            g.write(tail)
        finally:
            g.close()
        log("janitor: trimmed %s" % path)
    except Exception as e:
        log("janitor: could not trim %s: %s" % (path, e))

def load_json(path):
    try:
        f = open(path)
        try:
            return json.load(f)
        finally:
            f.close()
    except Exception:
        return None

def save_json(path, obj):
    """Write to a temporary file and rename, so a power cut mid-write leaves
    the previous copy intact rather than a truncated one."""
    tmp = path + ".tmp"
    try:
        f = open(tmp, "w")
        try:
            json.dump(obj, f, separators=(",", ":"))
        finally:
            f.close()
        os.rename(tmp, path)
    except Exception as e:
        log("save %s failed: %s" % (path, e))

def load_tariff(path=None):
    """The built-in tariff with tariff.json laid over it key by key (the
    price tables merge, so a file can just add next month's fuel figure).
    A missing or broken file leaves the built-in values in place."""
    t = json.loads(json.dumps(DEFAULT_TARIFF))
    data = load_json(path or TARIFF_PATH)
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(t.get(k), dict):
                t[k].update(v)
            else:
                t[k] = v
    if not (isinstance(t.get("hours"), list) and len(t["hours"]) == 24):
        t["hours"] = list(DEFAULT_TARIFF["hours"])
    return t

def restore_state(state):
    days = load_json(DAILY_PATH)
    if isinstance(days, dict):
        for d, kwh in days.items():
            if isinstance(kwh, list) and len(kwh) == 48:
                state.days[str(d)] = kwh
    ring = load_json(RING_PATH)
    if isinstance(ring, list):
        cutoff = time.time() - 86400
        for sample in ring:
            if isinstance(sample, list) and len(sample) == 4 and sample[0] >= cutoff:
                state.history.append(tuple(sample))
    log("restored %d day(s) of half-hour history, %d per-minute sample(s)"
        % (len(state.days), len(state.history)))

def persister(state):
    last_ring = time.time()
    while True:
        time.sleep(30)
        with state.lock:
            days = dict(state.days) if state.days_dirty else None
            state.days_dirty = False
        if days is not None:
            save_json(DAILY_PATH, days)
        if time.time() - last_ring >= RING_SAVE_SEC:
            save_json(RING_PATH, [list(s) for s in state.get_history()])
            last_ring = time.time()

def janitor():
    while True:
        time.sleep(JANITOR_SEC)
        trim_log(BRIDGE_LOG, LOG_MAX, LOG_KEEP)
        trim_log(LOG_PATH, LOG_MAX, LOG_KEEP)

# ---------------------------------------------------------------------------
# Accept loops
# ---------------------------------------------------------------------------

def serve_forever(host, port, handler, state, tag):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except Exception as e:
        log("%s: cannot bind %s:%d (%s) - is something already using that port?"
            % (tag, host, port, e))
        raise
    s.listen(16)
    log("%s: listening on %s:%d" % (tag, host, port))
    while True:
        try:
            conn, addr = s.accept()
        except Exception as e:
            log("%s: accept error: %s" % (tag, e))
            time.sleep(1)
            continue
        t = threading.Thread(target=handler, args=(conn, addr, state))
        t.daemon = True
        t.start()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _log_file
    try:
        _log_file = open(LOG_PATH, "a")
    except Exception:
        pass

    cfg = {}
    try:
        f = open(CONFIG_PATH)
        try:
            cfg = json.load(f)
        finally:
            f.close()
    except Exception as e:
        log("config load failed (%s); falling back to defaults" % e)

    hub_port    = int(cfg.get("mqtt_port", DEFAULT_HUB_PORT))
    http_port   = int(cfg.get("http_port", DEFAULT_HTTP_PORT))
    history_len = int(cfg.get("history_size", DEFAULT_HISTORY))

    state = State(history_len)
    restore_state(state)
    log("=== meter_hub start (mqtt=127.0.0.1:%d http=0.0.0.0:%d history=%d) ==="
        % (hub_port, http_port, history_len))

    t_mqtt = threading.Thread(target=serve_forever,
                              args=("127.0.0.1", hub_port, handle_mqtt_client, state, "mqtt"))
    t_mqtt.daemon = True
    t_mqtt.start()

    t_jan = threading.Thread(target=janitor)
    t_jan.daemon = True
    t_jan.start()

    t_save = threading.Thread(target=persister, args=(state,))
    t_save.daemon = True
    t_save.start()

    # HTTP runs on the main thread: if it dies the process exits and init
    # restarts the whole service, which is the behaviour we want.
    serve_forever("0.0.0.0", http_port, handle_http_client, state, "http")

# ---------------------------------------------------------------------------
# Dashboard page (served verbatim; no string formatting is applied to it)
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f9f9f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d0d0d" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>電力モニタ</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --base:#c3c2b7; --ring:rgba(11,11,11,.10); --hover:rgba(11,11,11,.05);
  --s1:#2a78d6; --s1wash:rgba(42,120,214,.10); --s1soft:rgba(42,120,214,.38);
  --deemph:#898781;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --tipbg:#0b0b0b; --tipfg:#ffffff; --tipdim:#c3c2b7;
}
@media (prefers-color-scheme: dark){
  :root{
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --base:#383835; --ring:rgba(255,255,255,.10); --hover:rgba(255,255,255,.06);
    --s1:#3987e5; --s1wash:rgba(57,135,229,.12); --s1soft:rgba(57,135,229,.42);
    --tipbg:#ffffff; --tipfg:#0b0b0b; --tipdim:#52514e;
  }
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--plane);color:var(--ink)}
body{
  font-family:system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
  -webkit-text-size-adjust:100%;line-height:1.45;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
}
.wrap{max-width:560px;margin:0 auto;padding:12px 14px 32px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13px;color:var(--ink2);padding:4px 2px 0}
.status{display:inline-flex;align-items:center;gap:6px;min-width:0}
.status span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.good{background:var(--good)} .dot.warning{background:var(--warning)}
.dot.serious{background:var(--serious)} .dot.critical{background:var(--critical)}
.hero{padding:20px 2px 16px}
.hero .label{font-size:13px;color:var(--ink2)}
.hero .value{font-size:clamp(56px,19vw,96px);font-weight:600;line-height:1.05;letter-spacing:-.02em;margin-top:2px}
.hero .value .u{font-size:.3em;font-weight:500;color:var(--ink2);margin-left:.2em;letter-spacing:0}
.level{display:inline-flex;align-items:center;gap:6px;margin-top:6px;font-size:14px;color:var(--ink2)}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:12px}
.tile,.card{background:var(--surface);border:1px solid var(--ring);border-radius:14px}
.tile{padding:11px 11px 10px}
.tiles.in{margin:4px 0 6px}
.tiles.in .tile{background:var(--plane);border-color:transparent}
.note{font-size:11.5px;color:var(--ink2);margin:4px 0 8px}
.tile .k{font-size:12px;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .v{font-size:22px;font-weight:600;margin-top:2px;white-space:nowrap}
.tile .v small{font-size:12px;font-weight:500;color:var(--ink2);margin-left:2px}
.tile .d{font-size:11.5px;color:var(--ink2);margin-top:2px;min-height:1.45em}
.arrow{font-size:9px;margin-right:3px;vertical-align:1px}
.arrow.up{color:var(--serious)} .arrow.down{color:var(--good)}
.card{margin:0 0 12px;padding:13px 14px 8px;transition:opacity .25s}
.card.dim{opacity:.55}
.card h2{font-size:15px;font-weight:600;margin:0}
.cap{display:flex;justify-content:space-between;align-items:baseline;gap:4px 10px;flex-wrap:wrap;margin-bottom:6px}
.sub{font-size:12px;color:var(--ink2)}
.legend{display:flex;gap:12px;font-size:12px;color:var(--ink2)}
.key{display:inline-flex;align-items:center;gap:5px}
.sw{display:inline-block}
.sw.bar{width:10px;height:10px;border-radius:2px;background:var(--s1)}
.sw.line{width:14px;height:0;border-top:2px solid var(--deemph)}
.chart{position:relative;outline:none;touch-action:pan-y;-webkit-tap-highlight-color:transparent}
.chart:focus-visible{box-shadow:0 0 0 2px var(--s1);border-radius:6px}
.chart svg{display:block;width:100%;overflow:visible}
.empty{font-size:13px;color:var(--muted);padding:40px 0;text-align:center}
.grid{stroke:var(--grid);stroke-width:1}
.baseline{stroke:var(--base);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.bar{fill:var(--s1)} .bar.soft{fill:var(--s1soft)}
.colhover{fill:var(--hover)}
.yline{fill:none;stroke:var(--deemph);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.pline{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.parea{fill:var(--s1wash);stroke:none}
.enddot{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.cross{stroke:var(--base);stroke-width:1}
.ref{stroke:var(--muted);stroke-width:1}
.reflabel{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.caplabel{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}
.halo{paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
.tip{position:absolute;top:0;pointer-events:none;background:var(--tipbg);color:var(--tipfg);border-radius:8px;
  padding:6px 9px;font-size:12px;line-height:1.35;white-space:nowrap;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:2}
.tip .t{color:var(--tipdim);font-size:11px;margin-bottom:2px}
.tip .r{display:flex;align-items:center;gap:6px}
.tip .r b{font-weight:600;font-variant-numeric:tabular-nums}
.tip .lk{display:inline-block;width:10px;height:0;border-top:2px solid}
.tip .lk.s1{border-color:var(--s1)} .tip .lk.deemph{border-color:var(--deemph)}
details{margin-top:2px}
summary{font-size:12px;color:var(--ink2);cursor:pointer;padding:4px 0}
.tw{max-height:260px;overflow:auto;margin:4px 0 6px;border-top:1px solid var(--grid)}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:500;position:sticky;top:0;background:var(--surface)}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px;padding:4px 0 8px}
.kv .k{font-size:12px;color:var(--ink2)}
.kv .v{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
footer{text-align:center;font-size:11px;color:var(--muted);margin-top:16px}
@media (max-width:370px){
  .tiles{grid-template-columns:1fr 1fr}
  .tiles .tile:last-child{grid-column:1 / -1}
}
</style>
</head>
<body>
<main class="wrap">

  <div class="top">
    <span class="status"><span class="dot" id="liveDot"></span><span id="liveText">Cube に接続しています…</span></span>
    <span id="clock"></span>
  </div>

  <section class="hero">
    <div class="label">いまの消費電力</div>
    <div class="value"><span id="pw">----</span><span class="u">W</span></div>
    <div class="level"><span class="dot" id="levelDot"></span><span id="levelText">-</span></div>
  </section>

  <section class="tiles">
    <div class="tile"><div class="k">今日（ここまで）</div><div class="v" id="tToday">--<small>kWh</small></div><div class="d" id="tTodayD"></div></div>
    <div class="tile"><div class="k">昨日</div><div class="v" id="tYday">--<small>kWh</small></div><div class="d" id="tYdayD"></div></div>
    <div class="tile"><div class="k">直近7日の平均</div><div class="v" id="tAvg">--<small>kWh</small></div><div class="d" id="tAvgD"></div></div>
  </section>

  <section class="card" id="cardCost">
    <div class="cap"><h2>電気代のめやす</h2><span class="sub" id="costSub"></span></div>
    <div class="tiles in">
      <div class="tile"><div class="k">今日</div><div class="v" id="cToday">--<small>円</small></div><div class="d" id="cTodayD"></div></div>
      <div class="tile"><div class="k">昨日</div><div class="v" id="cYday">--<small>円</small></div><div class="d" id="cYdayD"></div></div>
      <div class="tile"><div class="k" id="cMonthK">今月分の見込み</div><div class="v" id="cMonth">--<small>円</small></div><div class="d" id="cMonthD"></div></div>
    </div>
    <details><summary>内訳を見る</summary><div class="tw" id="costTable"></div><p class="note" id="costNote"></p></details>
  </section>

  <figure class="card" id="cardSlots">
    <div class="cap">
      <h2>今日の30分ごと</h2>
      <div class="legend"><span class="key"><span class="sw bar"></span>今日</span><span class="key"><span class="sw line"></span>昨日</span></div>
    </div>
    <div class="chart" id="slotChart" tabindex="0" role="img" aria-label="今日の30分ごとの使用量"></div>
    <details><summary>表で見る</summary><div class="tw" id="slotTable"></div></details>
  </figure>

  <figure class="card" id="cardPower">
    <div class="cap"><h2>直近24時間の電力</h2><span class="sub" id="powerSub"></span></div>
    <div class="chart" id="powerChart" tabindex="0" role="img" aria-label="直近24時間の消費電力"></div>
    <details><summary>表で見る（1時間ごと）</summary><div class="tw" id="powerTable"></div></details>
  </figure>

  <figure class="card" id="cardDaily">
    <div class="cap"><h2>日ごとの使用量</h2><span class="sub" id="dailySub"></span></div>
    <div class="chart" id="dailyChart" tabindex="0" role="img" aria-label="日ごとの使用量"></div>
    <details><summary>表で見る</summary><div class="tw" id="dailyTable"></div></details>
  </figure>

  <section class="card" id="cardMore">
    <div class="cap"><h2>くわしく</h2></div>
    <div class="kv">
      <div><div class="k">電流 R相</div><div class="v" id="kR">--</div></div>
      <div><div class="k">電流 T相</div><div class="v" id="kT">--</div></div>
      <div><div class="k">積算・買電（通算）</div><div class="v" id="kEF">--</div></div>
      <div><div class="k">積算・売電（通算）</div><div class="v" id="kER">--</div></div>
      <div><div class="k">メーター値の時刻</div><div class="v" id="kTs">--</div></div>
      <div><div class="k">モニタの稼働時間</div><div class="v" id="kUp">--</div></div>
    </div>
  </section>

  <footer>Cube J1 ローカル電力モニタ</footer>
</main>

<script>
(function(){
"use strict";

var JST = 9 * 3600;
var WD = ["日", "月", "火", "水", "木", "金", "土"];
var NS = "http://www.w3.org/2000/svg";
var S = {
  latest: null, okAt: 0, fails: 0,
  hist: [], histMax: 0, histOkAt: 0,
  daily: null, dailyOkAt: 0, dailySlot: -1,
  tariff: null,
  busy: {},
  ch: { slot: {sel: -1}, power: {sel: null, idx: -1}, daily: {sel: -1} }
};

/* ---------- small helpers ---------- */

function $(id){ return document.getElementById(id); }
function pad2(n){ return (n < 10 ? "0" : "") + n; }
function isNum(v){ return typeof v === "number" && isFinite(v); }
function clear(n){ while (n.firstChild) n.removeChild(n.firstChild); }
function safe(fn){ try { fn(); } catch (e) { if (window.console) console.error(e); } }

function comma(s){
  var neg = s.charAt(0) === "-", out = "";
  if (neg) s = s.slice(1);
  while (s.length > 3){ out = "," + s.slice(-3) + out; s = s.slice(0, -3); }
  return (neg ? "-" : "") + s + out;
}
function fmt(v, digits){
  if (!isNum(v)) return "--";
  var s = v.toFixed(digits || 0), i = s.indexOf(".");
  return i < 0 ? comma(s) : comma(s.slice(0, i)) + s.slice(i);
}
function tickDigits(step){
  for (var d = 0; d <= 3; d++){
    var p = Math.pow(10, d);
    if (Math.abs(Math.round(step * p) - step * p) < 1e-6) return d;
  }
  return 2;
}
function niceMax(v){
  if (!(v > 0)) return 1;
  var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10)), n = v / p;
  var m = n <= 1 ? 1 : n <= 1.5 ? 1.5 : n <= 2 ? 2 : n <= 3 ? 3 : n <= 4 ? 4 :
          n <= 5 ? 5 : n <= 6 ? 6 : n <= 8 ? 8 : 10;
  return m * p;
}

/* Times. The meter and its dates are Japan time; do the arithmetic on the
   Cube's clock (it is kept accurate) rather than trusting the phone's. */
function nowTs(){
  if (S.latest && isNum(S.latest.now)) return S.latest.now + (Date.now() - S.okAt) / 1000;
  return Date.now() / 1000;
}
function jst(ts){
  var d = new Date((ts + JST) * 1000);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(),
           h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds() };
}
function dateKey(ts){ var p = jst(ts); return p.y + "-" + pad2(p.mo) + "-" + pad2(p.d); }
function hm(ts){ var p = jst(ts); return pad2(p.h) + ":" + pad2(p.mi); }
function md(key){ var a = key.split("-"); return (+a[1]) + "/" + (+a[2]); }
function mdw(key){
  var a = key.split("-"), d = new Date(Date.UTC(+a[0], +a[1] - 1, +a[2]));
  return md(key) + "（" + WD[d.getUTCDay()] + "）";
}
function slotLabel(i){
  var a = i * 30, b = a + 30;
  return pad2(Math.floor(a / 60)) + ":" + pad2(a % 60) + "–" + pad2(Math.floor(b / 60)) + ":" + pad2(b % 60);
}
function ago(sec){
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + "秒前";
  if (sec < 3600) return Math.floor(sec / 60) + "分前";
  return Math.floor(sec / 3600) + "時間前";
}
function dur(s){
  if (s < 3600) return Math.floor(s / 60) + "分";
  if (s < 86400) return Math.floor(s / 3600) + "時間";
  return Math.floor(s / 86400) + "日" + Math.floor(s % 86400 / 3600) + "時間";
}

/* ---------- DOM / SVG building ---------- */

function sv(tag, attrs, parent){
  var e = document.createElementNS(NS, tag);
  for (var k in attrs){ if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, attrs[k]); }
  if (parent) parent.appendChild(e);
  return e;
}
function label(parent, x, y, str, cls, anchor){
  var t = sv("text", { x: x, y: y, "class": cls, "text-anchor": anchor || "start" }, parent);
  t.textContent = str;
  return t;
}
function barPath(x, top, w, base, r){
  var h = base - top;
  if (h <= 0.5) return "";
  r = Math.min(r, w / 2, h);
  return "M" + x + "," + base + "L" + x + "," + (top + r) +
         "Q" + x + "," + top + " " + (x + r) + "," + top +
         "L" + (x + w - r) + "," + top +
         "Q" + (x + w) + "," + top + " " + (x + w) + "," + (top + r) +
         "L" + (x + w) + "," + base + "Z";
}
function empty(box, msg){
  clear(box);
  var e = document.createElement("div");
  e.className = "empty";
  e.textContent = msg;
  box.appendChild(e);
}
function table(box, head, rows){
  clear(box);
  var tb = document.createElement("table"), th = document.createElement("thead"), tr = document.createElement("tr");
  for (var i = 0; i < head.length; i++){ var c = document.createElement("th"); c.textContent = head[i]; tr.appendChild(c); }
  th.appendChild(tr); tb.appendChild(th);
  var body = document.createElement("tbody");
  for (var r = 0; r < rows.length; r++){
    var row = document.createElement("tr");
    for (var j = 0; j < rows[r].length; j++){ var td = document.createElement("td"); td.textContent = rows[r][j]; row.appendChild(td); }
    body.appendChild(row);
  }
  tb.appendChild(body);
  box.appendChild(tb);
}
function setVal(id, val, unit){
  var el = $(id);
  clear(el);
  el.appendChild(document.createTextNode(val));
  var s = document.createElement("small");
  s.textContent = unit;
  el.appendChild(s);
}
function setNote(id, arrow, str){
  var el = $(id);
  clear(el);
  if (arrow){
    var a = document.createElement("span");
    a.className = "arrow " + arrow;
    a.textContent = arrow === "up" ? "▲" : "▼";
    el.appendChild(a);
  }
  el.appendChild(document.createTextNode(str));
}
function setDot(el, level){ el.className = "dot" + (level ? " " + level : ""); }
function dim(id, on){ var el = $(id); if (on) el.classList.add("dim"); else el.classList.remove("dim"); }

/* A chart frame: an SVG sized to its container in real pixels, plus one
   tooltip element. Re-created on every render. */
function frame(box, height){
  clear(box);
  var w = Math.max(260, Math.round(box.clientWidth || 320));
  var svg = sv("svg", { width: w, height: height, viewBox: "0 0 " + w + " " + height }, box);
  var tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  box.appendChild(tip);
  return { svg: svg, w: w, h: height, tip: tip };
}
function yAxis(f, left, right, top, bottom, max){
  var digits = tickDigits(max / 2);
  for (var g = 0; g <= 2; g++){
    var y = bottom - (bottom - top) * g / 2;
    sv("line", { x1: left, x2: right, y1: y, y2: y, "class": g === 0 ? "baseline" : "grid" }, f.svg);
    label(f.svg, left - 6, y + 4, fmt(max * g / 2, digits), "tick", "end");
  }
}
function showTip(f, xPx, title, rows){
  var t = f.tip;
  clear(t);
  var h = document.createElement("div");
  h.className = "t";
  h.textContent = title;
  t.appendChild(h);
  for (var i = 0; i < rows.length; i++){
    var r = document.createElement("div");
    r.className = "r";
    if (rows[i].key){ var k = document.createElement("span"); k.className = "lk " + rows[i].key; r.appendChild(k); }
    var b = document.createElement("b");
    b.textContent = rows[i].value;
    r.appendChild(b);
    if (rows[i].label){ var l = document.createElement("span"); l.textContent = rows[i].label; r.appendChild(l); }
    t.appendChild(r);
  }
  t.hidden = false;
  var tw = t.offsetWidth;
  t.style.left = Math.max(0, Math.min(f.w - tw, xPx - tw / 2)) + "px";
}
function bindPointer(f, onMove, onLeave){
  var el = f.svg;
  function pos(ev){
    var r = el.getBoundingClientRect();
    var p = ev.touches && ev.touches.length ? ev.touches[0] : ev;
    return (p.clientX - r.left) * (f.w / r.width);
  }
  if (window.PointerEvent){
    el.addEventListener("pointermove", function(ev){ onMove(pos(ev)); });
    el.addEventListener("pointerdown", function(ev){ onMove(pos(ev)); });
    el.addEventListener("pointerleave", function(ev){ if (ev.pointerType === "mouse") onLeave(); });
  } else {
    el.addEventListener("mousemove", function(ev){ onMove(pos(ev)); });
    el.addEventListener("mouseleave", onLeave);
    el.addEventListener("touchstart", function(ev){ onMove(pos(ev)); });
  }
}

/* ---------- derived numbers ---------- */

function totals(){
  var t = nowTs(), p = jst(t);
  var mins = p.h * 60 + p.mi + p.s / 60;
  var slot = Math.min(47, Math.floor(mins / 30)), frac = (mins - slot * 30) / 30;
  var days = S.daily || [], map = {};
  for (var i = 0; i < days.length; i++) map[days[i].date] = days[i];
  var todayKey = dateKey(t);
  var td = map[todayKey] || null, yd = map[dateKey(t - 86400)] || null;
  var live = S.latest && isNum(S.latest.energy_forward_kwh) ? S.latest.energy_forward_kwh : null;
  var today = null, ysame = null, partial = null;
  if (td){
    // The live meter total minus today's midnight reading is more current
    // than the half-hour history, which lags by up to thirty minutes.
    today = (live !== null && isNum(td.cum_kwh[0])) ? Math.max(0, live - td.cum_kwh[0]) : td.total_kwh;
    if (live !== null && isNum(td.cum_kwh[slot])) partial = Math.max(0, live - td.cum_kwh[slot]);
  }
  if (yd && isNum(yd.cum_kwh[0]) && isNum(yd.cum_kwh[slot])){
    var a = yd.cum_kwh[slot], b = slot < 47 ? yd.cum_kwh[slot + 1] : null;
    ysame = a - yd.cum_kwh[0] + (isNum(b) ? (b - a) * frac : 0);
  }
  var n = 0, sum = 0;
  for (var j = days.length - 1; j >= 0 && n < 7; j--){
    if (days[j].date < todayKey && days[j].complete && isNum(days[j].total_kwh)){ sum += days[j].total_kwh; n++; }
  }
  return { t: t, slot: slot, todayKey: todayKey, td: td, yd: yd, today: today, ysame: ysame,
           partial: partial, avg: n ? sum / n : null, avgN: n };
}

/* ---------- electricity price ----------
   Rates come from /api/tariff (the Cube's tariff.json over the built-in
   電化上手 table). Unit prices include tax and the fuel adjustment already
   contains the government discount. This is an estimate: the real bill
   applies the monthly total's rounding. */

function billMonth(key){
  var y = +key.slice(0, 4), m = +key.slice(5, 7), d = +key.slice(8, 10);
  if (d >= S.tariff.meter_day){ m++; if (m > 12){ m = 1; y++; } }
  return y + "-" + pad2(m);
}
function periodOf(key){
  var md0 = S.tariff.meter_day, y = +key.slice(0, 4), m = +key.slice(5, 7), d = +key.slice(8, 10);
  if (d < md0){ m--; if (m < 1){ m = 12; y--; } }
  var a = Date.UTC(y, m - 1, md0), b = Date.UTC(m === 12 ? y + 1 : y, m === 12 ? 0 : m, md0);
  return { start: dateKey(a / 1000), end: dateKey(b / 1000 - 86400), days: Math.round((b - a) / 86400000) };
}
function fuelFor(month){
  var f = S.tariff.fuel_yen_kwh || {}, best = null;
  if (isNum(f[month])) return { v: f[month], month: month, guess: false };
  for (var k in f){ if (isNum(f[k]) && (best === null || k > best)) best = k; }
  return best === null ? { v: 0, month: null, guess: true } : { v: f[best], month: best, guess: true };
}
function rateOf(band, key){
  var TF = S.tariff, m = +key.slice(5, 7);
  if (TF.summer_months.indexOf(m) >= 0 && TF.summer_rates && isNum(TF.summer_rates[band])) return TF.summer_rates[band];
  return TF.rates[band];
}
/* One day's price. extra is usage the half-hour history does not hold yet
   (today's live meter total past the last recorded slot), charged at the
   rate of extraSlot. */
function priceDay(day, extra, extraSlot){
  var out = { kwh: 0, energy: 0, bands: {}, fuel: fuelFor(billMonth(day.date)) };
  function add(slot, u){
    var b = S.tariff.hours[Math.floor(slot / 2)], r = rateOf(b, day.date);
    if (!out.bands[b]) out.bands[b] = { kwh: 0, yen: 0, rate: r };
    out.bands[b].kwh += u; out.bands[b].yen += u * r;
    out.kwh += u; out.energy += u * r;
  }
  for (var i = 0; i < 48; i++) if (isNum(day.use_kwh[i])) add(i, day.use_kwh[i]);
  if (isNum(extra) && extra > 0) add(extraSlot, extra);
  out.fuelYen = out.kwh * out.fuel.v;
  out.renewYen = out.kwh * S.tariff.renewable_yen_kwh;
  out.yen = out.energy + out.fuelYen + out.renewYen;
  return out;
}
function todayPrice(T){
  if (!T.td) return null;
  var known = 0;
  for (var i = 0; i < 48; i++) if (isNum(T.td.use_kwh[i])) known += T.td.use_kwh[i];
  return priceDay(T.td, isNum(T.today) ? T.today - known : null, T.slot);
}

/* ---------- renderers ---------- */

function renderHero(){
  var d = S.latest, now = Date.now(), w = d ? d.power_w : null;
  $("pw").textContent = isNum(w) ? fmt(w) : "----";
  if (isNum(w)){
    var lv = w >= 3000 ? ["serious", "かなり多め"] : w >= 1200 ? ["warning", "多め"] : ["good", "ふつう"];
    setDot($("levelDot"), lv[0]);
    $("levelText").textContent = lv[1];
  } else {
    setDot($("levelDot"), "");
    $("levelText").textContent = "-";
  }
  var dot = $("liveDot"), txt = $("liveText");
  if (!S.okAt){
    setDot(dot, "");
    txt.textContent = "Cube に接続しています…";
  } else if (now - S.okAt > 35000){
    setDot(dot, "critical");
    txt.textContent = "Cube に接続できません（最終 " + ago((now - S.okAt) / 1000) + "）";
  } else if (!isNum(d.ts) || d.now - d.ts > 180){
    setDot(dot, "warning");
    txt.textContent = "メーターの応答を待っています";
  } else {
    setDot(dot, "good");
    txt.textContent = "受信中・" + ago(d.now - d.ts + (now - S.okAt) / 1000);
  }
  $("clock").textContent = hm(nowTs());
}

function renderTiles(){
  var T = totals();
  setVal("tToday", fmt(T.today, 1), "kWh");
  if (isNum(T.today) && isNum(T.ysame) && T.ysame > 0.05){
    var pct = Math.round((T.today - T.ysame) / T.ysame * 100);
    if (Math.abs(pct) < 3) setNote("tTodayD", null, "昨日並み");
    else setNote("tTodayD", pct > 0 ? "up" : "down", "昨日比 " + (pct > 0 ? "+" : "") + pct + "%");
  } else {
    setNote("tTodayD", null, "");
  }
  setVal("tYday", T.yd ? fmt(T.yd.total_kwh, 1) : "--", "kWh");
  setNote("tYdayD", null, T.yd ? (T.yd.complete ? mdw(T.yd.date) : "一部欠け") : "");
  setVal("tAvg", fmt(T.avg, 1), "kWh");
  setNote("tAvgD", null, T.avgN ? (T.avgN < 7 ? T.avgN + "日ぶん" : "1日あたり") : "");
}

function renderMore(){
  var d = S.latest;
  if (!d) return;
  $("kR").textContent = fmt(d.current_r_a, 1) + " A";
  $("kT").textContent = fmt(d.current_t_a, 1) + " A";
  $("kEF").textContent = fmt(d.energy_forward_kwh, 1) + " kWh";
  $("kER").textContent = fmt(d.energy_reverse_kwh, 1) + " kWh";
  $("kTs").textContent = isNum(d.ts) ? hm(d.ts) : "--";
  $("kUp").textContent = isNum(d.uptime_s) ? dur(d.uptime_s) : "--";
}

function renderCost(){
  if (!S.tariff || !S.daily) return;
  var TF = S.tariff, T = totals(), tp = todayPrice(T), yp = T.yd ? priceDay(T.yd) : null;
  setVal("cToday", tp ? fmt(tp.yen) : "--", "円");
  setNote("cTodayD", null, tp && tp.kwh ? fmt(tp.yen / tp.kwh, 1) + " 円/kWh" : "");
  setVal("cYday", yp ? fmt(yp.yen) : "--", "円");
  setNote("cYdayD", null, yp ? (T.yd.complete ? fmt(yp.yen / yp.kwh, 1) + " 円/kWh" : "一部欠け") : "");

  // The billing period so far, from what the Cube has recorded.
  var P = periodOf(T.todayKey), sum = { kwh: 0, fuelYen: 0, renewYen: 0, yen: 0, bands: {} };
  var rec = 0, full = 0, fullYen = 0, fuel = null;
  for (var j = 0; j < S.daily.length; j++){
    var d = S.daily[j];
    if (d.date < P.start || d.date > T.todayKey) continue;
    var p = d.date === T.todayKey ? tp : priceDay(d);
    if (!p || !p.kwh) continue;
    rec++; fuel = p.fuel;
    if (d.complete && d.date !== T.todayKey){ full++; fullYen += p.yen; }
    sum.kwh += p.kwh; sum.fuelYen += p.fuelYen; sum.renewYen += p.renewYen; sum.yen += p.yen;
    for (var b in p.bands){
      if (!sum.bands[b]) sum.bands[b] = { kwh: 0, yen: 0 };
      sum.bands[b].kwh += p.bands[b].kwh; sum.bands[b].yen += p.bands[b].yen;
      sum.bands[b].rate = p.bands[b].rate;
    }
  }
  var bm = billMonth(T.todayKey);
  $("cMonthK").textContent = (+bm.slice(5)) + "月分の見込み";
  // Basic charge plus the average recorded full day, over the whole period.
  setVal("cMonth", full ? fmt(TF.basic_yen + fullYen / full * P.days) : "--", "円");
  // How many days are recorded is in the breakdown table; the tile has no room.
  setNote("cMonthD", null, md(P.start) + "〜" + md(P.end));
  $("costSub").textContent = TF.name || "";

  var rows = [], order = TF.band_order || [];
  for (var k = 0; k < order.length; k++){
    var s = sum.bands[order[k]];
    if (s) rows.push([(TF.labels && TF.labels[order[k]]) || order[k], fmt(s.kwh, 1), fmt(s.rate, 2), fmt(s.yen)]);
  }
  if (fuel){
    rows.push(["燃料費調整" + (fuel.guess && fuel.month ? "（" + (+fuel.month.slice(5)) + "月分の値で仮）" : ""),
               fmt(sum.kwh, 1), fmt(fuel.v, 2), fmt(sum.fuelYen)]);
  }
  rows.push(["再エネ賦課金", fmt(sum.kwh, 1), fmt(TF.renewable_yen_kwh, 2), fmt(sum.renewYen)]);
  rows.push(["記録のある" + rec + "日の計", fmt(sum.kwh, 1), "", fmt(sum.yen)]);
  rows.push(["基本料金（1か月）", "", "", fmt(TF.basic_yen)]);
  table($("costTable"), [(+bm.slice(5)) + "月分", "kWh", "円/kWh", "円"], rows);
  $("costNote").textContent = "税込のめやすです。燃料費調整は国の値引きを含みます。" +
    "請求額は月の合計で端数を処理するため、少しずれます。単価は " + (TF.checked || "") + " 時点。";
}

function renderSlot(){
  var box = $("slotChart");
  if (!S.daily){ empty(box, "履歴を読み込んでいます…"); return; }
  var T = totals();
  if (!T.td && !T.yd){ empty(box, "今日と昨日の記録がまだありません"); return; }

  var f = frame(box, 196);
  var L = 38, R = f.w - 4, top = 30, bottom = f.h - 22, band = (R - L) / 48;
  var today = [], yday = [], maxv = 0;
  for (var i = 0; i < 48; i++){
    var v = T.td ? T.td.use_kwh[i] : null, soft = false;
    if (!isNum(v) && i === T.slot && isNum(T.partial)){ v = T.partial; soft = true; }
    var y = T.yd ? T.yd.use_kwh[i] : null;
    today.push({ v: isNum(v) ? v : null, soft: soft });
    yday.push(isNum(y) ? y : null);
    if (isNum(v)) maxv = Math.max(maxv, v);
    if (isNum(y)) maxv = Math.max(maxv, y);
  }
  var max = niceMax(maxv * 1.08 || 0.5);
  function Y(val){ return bottom - (val / max) * (bottom - top); }

  yAxis(f, L, R, top, bottom, max);
  var hover = sv("rect", { x: 0, y: top, width: 0, height: bottom - top, "class": "colhover", visibility: "hidden" }, f.svg);

  var bw = Math.max(2, Math.min(24, band - 2));
  for (var k = 0; k < 48; k++){
    if (today[k].v === null) continue;
    var d = barPath(L + k * band + (band - bw) / 2, Y(today[k].v), bw, bottom, 4);
    if (d) sv("path", { d: d, "class": today[k].soft ? "bar soft" : "bar" }, f.svg);
  }
  var line = "", pen = false;
  for (var m = 0; m < 48; m++){
    if (yday[m] === null){ pen = false; continue; }
    line += (pen ? "L" : "M") + (L + m * band + band / 2).toFixed(1) + "," + Y(yday[m]).toFixed(1);
    pen = true;
  }
  if (line) sv("path", { d: line, "class": "yline" }, f.svg);

  var hours = [0, 6, 12, 18, 24];
  for (var h = 0; h < hours.length; h++){
    label(f.svg, L + hours[h] * 2 * band, f.h - 6, hours[h] + "時", "tick",
          h === 0 ? "start" : h === hours.length - 1 ? "end" : "middle");
  }

  function show(i){
    i = Math.max(0, Math.min(47, i));
    S.ch.slot.sel = i;
    hover.setAttribute("x", L + i * band);
    hover.setAttribute("width", band);
    hover.setAttribute("visibility", "visible");
    showTip(f, L + i * band + band / 2, slotLabel(i), [
      { key: "s1", value: today[i].v === null ? "記録待ち" : fmt(today[i].v, 2) + " kWh", label: today[i].soft ? "今日（途中）" : "今日" },
      { key: "deemph", value: yday[i] === null ? "記録なし" : fmt(yday[i], 2) + " kWh", label: "昨日" }
    ]);
  }
  function hide(){ S.ch.slot.sel = -1; hover.setAttribute("visibility", "hidden"); f.tip.hidden = true; }
  bindPointer(f, function(x){ show(Math.floor((x - L) / band)); }, hide);
  S.ch.slot.n = 48; S.ch.slot.show = show; S.ch.slot.hide = hide;
  if (S.ch.slot.sel >= 0) show(S.ch.slot.sel);

  box.setAttribute("aria-label", "今日の30分ごとの使用量。ここまでの合計 " + fmt(T.today, 1) + " kWh");
  var rows = [];
  for (var q = 47; q >= 0; q--){
    if (today[q].v === null && yday[q] === null) continue;
    rows.push([slotLabel(q),
               today[q].v === null ? "—" : fmt(today[q].v, 2) + (today[q].soft ? "（途中）" : ""),
               yday[q] === null ? "—" : fmt(yday[q], 2)]);
  }
  table($("slotTable"), ["時間帯", "今日 kWh", "昨日 kWh"], rows);
}

function renderPower(){
  var box = $("powerChart"), now = nowTs(), t0 = now - 86400, pts = [];
  for (var i = 0; i < S.hist.length; i++){
    var s = S.hist[i];
    if (s[0] >= t0 && isNum(s[1])) pts.push(s);
  }
  if (pts.length < 2){
    empty(box, S.histOkAt ? "データを集めています" : "読み込んでいます…");
    $("powerSub").textContent = "";
    return;
  }
  var f = frame(box, 190);
  var L = 44, R = f.w - 8, top = 30, bottom = f.h - 22, pw = R - L;
  var maxv = 0, maxAt = pts[0];
  for (var j = 0; j < pts.length; j++){ if (pts[j][1] > maxv){ maxv = pts[j][1]; maxAt = pts[j]; } }
  var max = niceMax(maxv * 1.08);
  function X(t){ return L + (t - t0) / 86400 * pw; }
  function Y(v){ return bottom - (v / max) * (bottom - top); }

  yAxis(f, L, R, top, bottom, max);
  var first = Math.ceil((t0 + JST) / 21600) * 21600 - JST;
  for (var tt = first; tt <= now; tt += 21600){
    var tx = X(tt);
    if (tx < L + 14 || tx > R - 30) continue;
    label(f.svg, tx, f.h - 6, jst(tt).h + "時", "tick", "middle");
  }
  label(f.svg, R, f.h - 6, "いま", "tick", "end");

  var line = "", area = "", prev = null;
  for (var k = 0; k < pts.length; k++){
    var p = pts[k], x = X(p[0]).toFixed(1), y = Y(p[1]).toFixed(1);
    if (prev === null || p[0] - prev[0] > 300){
      if (prev !== null) area += "L" + X(prev[0]).toFixed(1) + "," + bottom + "Z";
      line += "M" + x + "," + y;
      area += "M" + x + "," + bottom + "L" + x + "," + y;
    } else {
      line += "L" + x + "," + y;
      area += "L" + x + "," + y;
    }
    prev = p;
  }
  area += "L" + X(prev[0]).toFixed(1) + "," + bottom + "Z";
  sv("path", { d: area, "class": "parea" }, f.svg);
  sv("path", { d: line, "class": "pline" }, f.svg);
  var cross = sv("line", { x1: 0, x2: 0, y1: top, y2: bottom, "class": "cross", visibility: "hidden" }, f.svg);
  var last = pts[pts.length - 1];
  sv("circle", { cx: X(last[0]), cy: Y(last[1]), r: 4, "class": "enddot" }, f.svg);
  var hdot = sv("circle", { cx: 0, cy: 0, r: 4, "class": "enddot", visibility: "hidden" }, f.svg);
  $("powerSub").textContent = "最大 " + fmt(maxv) + " W（" + hm(maxAt[0]) + "）";

  function nearest(t){
    var lo = 0, hi = pts.length - 1;
    while (hi - lo > 1){ var mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid; else hi = mid; }
    return (t - pts[lo][0] <= pts[hi][0] - t) ? lo : hi;
  }
  function show(i){
    i = Math.max(0, Math.min(pts.length - 1, i));
    var q = pts[i], xx = X(q[0]);
    S.ch.power.sel = q[0];
    S.ch.power.idx = i;
    cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("visibility", "visible");
    hdot.setAttribute("cx", xx); hdot.setAttribute("cy", Y(q[1])); hdot.setAttribute("visibility", "visible");
    showTip(f, xx, hm(q[0]), [{ key: "s1", value: fmt(q[1]) + " W", label: "電力" }]);
  }
  function hide(){
    S.ch.power.sel = null; S.ch.power.idx = -1;
    cross.setAttribute("visibility", "hidden"); hdot.setAttribute("visibility", "hidden");
    f.tip.hidden = true;
  }
  bindPointer(f, function(x){ show(nearest(t0 + (x - L) / pw * 86400)); }, hide);
  S.ch.power.n = pts.length; S.ch.power.show = show; S.ch.power.hide = hide;
  if (S.ch.power.sel !== null) show(nearest(S.ch.power.sel));

  var buckets = {}, order = [];
  for (var b = 0; b < pts.length; b++){
    var hk = Math.floor((pts[b][0] + JST) / 3600);
    if (!buckets[hk]){ buckets[hk] = { sum: 0, n: 0, max: 0 }; order.push(hk); }
    buckets[hk].sum += pts[b][1]; buckets[hk].n++; buckets[hk].max = Math.max(buckets[hk].max, pts[b][1]);
  }
  var rows = [];
  for (var o = order.length - 1; o >= 0; o--){
    var bk = buckets[order[o]];
    rows.push([hm(order[o] * 3600 - JST) + "〜", fmt(bk.sum / bk.n), fmt(bk.max)]);
  }
  table($("powerTable"), ["時刻", "平均 W", "最大 W"], rows);
}

function renderDaily(){
  var box = $("dailyChart");
  if (!S.daily){ empty(box, "履歴を読み込んでいます…"); return; }
  var T = totals(), items = [], src = S.daily.slice(-31);
  for (var i = 0; i < src.length; i++){
    var d = src[i], isToday = d.date === T.todayKey;
    var v = isToday ? T.today : d.total_kwh;
    if (!isNum(v)) continue;
    items.push({ date: d.date, v: v, today: isToday, partial: isToday || !d.complete, day: d });
  }
  if (!items.length){ empty(box, "日ごとの記録がまだありません"); $("dailySub").textContent = ""; return; }

  var f = frame(box, 206);
  var L = 38, R = f.w - 4, top = 24, bottom = f.h - 24, n = items.length;
  var band = (R - L) / Math.max(n, 10), offset = R - band * n;
  var maxv = 0, sum = 0, cnt = 0;
  for (var j = 0; j < n; j++){
    maxv = Math.max(maxv, items[j].v);
    if (!items[j].partial){ sum += items[j].v; cnt++; }
  }
  // The newest-day label lives in the strip above the plot, so the scale
  // needs only a little headroom.
  var max = niceMax(maxv * 1.04);
  function Y(val){ return bottom - (val / max) * (bottom - top); }

  yAxis(f, L, R, top, bottom, max);
  var hover = sv("rect", { x: 0, y: top, width: 0, height: bottom - top, "class": "colhover", visibility: "hidden" }, f.svg);
  var bw = Math.max(3, Math.min(24, band - 2));
  for (var k = 0; k < n; k++){
    var p = barPath(offset + k * band + (band - bw) / 2, Y(items[k].v), bw, bottom, 4);
    if (p) sv("path", { d: p, "class": items[k].partial ? "bar soft" : "bar" }, f.svg);
  }
  if (cnt){
    var avg = sum / cnt, ay = Y(avg);
    sv("line", { x1: L, x2: R, y1: ay, y2: ay, "class": "ref" }, f.svg);
    // Haloed so bars passing behind it do not break the text.
    label(f.svg, L + 4, ay - 5, "平均 " + fmt(avg, 1), "reflabel halo", "start");
  }
  var lastI = n - 1, li = items[lastI];
  label(f.svg, offset + lastI * band + (band + bw) / 2, top - 8,
        fmt(li.v, 1) + (li.today ? "（途中）" : ""), "caplabel halo", "end");

  var step = Math.max(1, Math.ceil(n / 6));
  for (var t = lastI; t >= 0; t -= step){
    label(f.svg, offset + t * band + band / 2, f.h - 6, md(items[t].date), "tick", t === lastI ? "end" : "middle");
  }

  function show(i){
    i = Math.max(0, Math.min(n - 1, i));
    var it = items[i];
    S.ch.daily.sel = i;
    hover.setAttribute("x", offset + i * band);
    hover.setAttribute("width", band);
    hover.setAttribute("visibility", "visible");
    var tipRows = [
      { key: "s1", value: fmt(it.v, 1) + " kWh", label: it.today ? "途中" : it.partial ? "一部欠け" : "" }
    ];
    if (S.tariff) tipRows.push({ value: yenOf(it) + " 円", label: "電気代のめやす" });
    showTip(f, offset + i * band + band / 2, mdw(it.date), tipRows);
  }
  function hide(){ S.ch.daily.sel = -1; hover.setAttribute("visibility", "hidden"); f.tip.hidden = true; }
  bindPointer(f, function(x){ show(Math.floor((x - offset) / band)); }, hide);
  S.ch.daily.n = n; S.ch.daily.show = show; S.ch.daily.hide = hide;
  if (S.ch.daily.sel >= 0) show(S.ch.daily.sel);

  $("dailySub").textContent = "直近" + n + "日";
  box.setAttribute("aria-label", "日ごとの使用量、直近" + n + "日");
  var rows = [];
  for (var r = n - 1; r >= 0; r--){
    rows.push([mdw(items[r].date), fmt(items[r].v, 1), yenOf(items[r]),
               items[r].today ? "途中" : items[r].partial ? "一部欠け" : ""]);
  }
  table($("dailyTable"), ["日付", "kWh", "円", ""], rows);

  function yenOf(it){
    if (!S.tariff) return "--";
    var p = it.today ? todayPrice(T) : priceDay(it.day);
    return p ? fmt(p.yen) : "--";
  }
}

/* ---------- fetching ----------
   The Wi-Fi link to the Cube drops a few percent of packets, which shows up
   as multi-second stalls. Never stack requests, give each a timeout, fetch
   history as a delta, and keep the previous render (dimmed) on failure. */

function get(url, key, timeout, ok, fail){
  if (S.busy[key]) return;
  S.busy[key] = true;
  var x = new XMLHttpRequest();
  x.open("GET", url, true);
  x.timeout = timeout;
  x.onloadend = function(){
    S.busy[key] = false;
    if (x.status === 200){
      var data;
      try { data = JSON.parse(x.responseText); } catch (e) { if (fail) fail(); return; }
      ok(data);
    } else if (fail) {
      fail();
    }
  };
  try { x.send(); } catch (e) { S.busy[key] = false; if (fail) fail(); }
}

function pollLatest(){
  get("/api", "latest", 9000, function(d){
    S.latest = d; S.okAt = Date.now(); S.fails = 0;
    safe(renderHero); safe(renderTiles); safe(renderCost); safe(renderMore); safe(renderSlot); safe(renderDaily);
    var T = totals(), p = jst(T.t);
    if (S.daily && T.slot !== S.dailySlot && (p.mi % 30) * 60 + p.s > 150) pollDaily();
  }, function(){ S.fails++; safe(renderHero); });
}

function pollHistory(){
  var since = S.histMax;
  get("/api/history" + (since ? "?since=" + since : ""), "hist", 25000, function(d){
    var add = d.samples || [], cut = nowTs() - 86400 - 900, k = 0;
    for (var i = 0; i < add.length; i++){
      if (add[i][0] > S.histMax){ S.hist.push(add[i]); S.histMax = add[i][0]; }
    }
    while (k < S.hist.length && S.hist[k][0] < cut) k++;
    if (k) S.hist.splice(0, k);
    S.histOkAt = Date.now();
    dim("cardPower", false);
    safe(renderPower);
  }, function(){ if (S.hist.length) dim("cardPower", true); });
}

function pollDaily(){
  get("/api/daily?days=31", "daily", 25000, function(d){
    S.daily = d.days || []; S.dailyOkAt = Date.now(); S.dailySlot = totals().slot;
    dim("cardSlots", false); dim("cardDaily", false); dim("cardCost", false);
    safe(renderTiles); safe(renderCost); safe(renderSlot); safe(renderDaily);
  }, function(){ if (S.daily){ dim("cardSlots", true); dim("cardDaily", true); dim("cardCost", true); } });
}

function pollTariff(){
  get("/api/tariff", "tariff", 20000, function(d){
    S.tariff = d;
    safe(renderCost); safe(renderDaily);
  });
}

/* ---------- wiring ---------- */

function keys(id, name){
  $(id).addEventListener("keydown", function(ev){
    var c = S.ch[name];
    if (!c.show) return;
    if (ev.key === "Escape"){ c.hide(); return; }
    var step = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    ev.preventDefault();
    var cur = name === "power" ? c.idx : c.sel;
    if (!(cur >= 0)) cur = step > 0 ? -1 : c.n;
    c.show(cur + step);
  });
  $(id).addEventListener("blur", function(){ var c = S.ch[name]; if (c.hide) c.hide(); });
}
keys("slotChart", "slot");
keys("powerChart", "power");
keys("dailyChart", "daily");

document.addEventListener(window.PointerEvent ? "pointerdown" : "touchstart", function(ev){
  for (var n = ev.target; n && n !== document; n = n.parentNode){
    if (n.classList && n.classList.contains("chart")) return;
  }
  for (var k in S.ch){ if (S.ch[k].hide) S.ch[k].hide(); }
}, true);

var resizeTimer = null;
window.addEventListener("resize", function(){
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function(){ safe(renderSlot); safe(renderPower); safe(renderDaily); }, 150);
});
document.addEventListener("visibilitychange", function(){
  if (!document.hidden){ pollLatest(); pollHistory(); pollDaily(); }
});

safe(renderHero); safe(renderSlot); safe(renderPower); safe(renderDaily);
pollLatest(); pollDaily(); pollHistory(); pollTariff();
setInterval(pollLatest, 10000);
// Prices change at most monthly: refetch hourly, or every minute until the first answer.
setInterval(function(){ if (!S.tariff || Date.now() % 3600000 < 60000) pollTariff(); }, 60000);
setInterval(pollHistory, 60000);
setInterval(function(){ if (!S.daily || Date.now() - S.dailyOkAt > 300000) pollDaily(); }, 30000);
setInterval(function(){ safe(renderHero); }, 5000);
})();
</script>
</body>
</html>
"""

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#0e1116"/>
<path d="M18.5 3 L8 18 h6 l-1.5 11 L24 14 h-6.5 z" fill="#3fb950"/>
</svg>
"""

if __name__ == "__main__":
    main()
