#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
meter_hub.py - Cube J1 local hub (no Home Assistant, no external broker)

The upstream mqtt_bridge.py is used unmodified. It is pointed at
127.0.0.1 and publishes there as usual. This process:

  1. speaks just enough MQTT 3.1.1 server-side to receive those publishes
  2. keeps the latest reading plus ~24h of history in RAM
  3. serves a phone-friendly page and a JSON API over plain HTTP

Deliberate constraints, because the Cube has Python 2.7 and no package
manager: only modules the bridge itself already proves are present are
used (os, sys, json, time, socket, struct, threading, collections).
No BaseHTTPServer, no SocketServer, no third-party packages.

Endpoints:
    GET /              phone dashboard (HTML)
    GET /api           latest reading (JSON)
    GET /api/history   ring buffer of recent samples (JSON)
    GET /healthz       "ok"
"""

import os
import sys
import json
import time
import socket
import struct
import binascii
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
            samples = [list(s) for s in state.get_history()]
            body = json.dumps({"samples": samples}, separators=(",", ":"))
            conn.sendall(http_response("200 OK", "application/json; charset=utf-8", body))
        elif path in ("/manifest.webmanifest", "/manifest.json"):
            conn.sendall(http_response("200 OK", "application/manifest+json; charset=utf-8", MANIFEST))
        elif path == "/icon-192.png":
            conn.sendall(http_response("200 OK", "image/png", ICON_192))
        elif path == "/icon-512.png":
            conn.sendall(http_response("200 OK", "image/png", ICON_512))
        elif path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            conn.sendall(http_response("200 OK", "image/png", ICON_180))
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
    log("=== meter_hub start (mqtt=127.0.0.1:%d http=0.0.0.0:%d history=%d) ==="
        % (hub_port, http_port, history_len))

    t_mqtt = threading.Thread(target=serve_forever,
                              args=("127.0.0.1", hub_port, handle_mqtt_client, state, "mqtt"))
    t_mqtt.daemon = True
    t_mqtt.start()

    t_jan = threading.Thread(target=janitor)
    t_jan.daemon = True
    t_jan.start()

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
<meta name="theme-color" content="#0e1116">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>電力モニタ</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="電力モニタ">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  :root{
    --bg:#0e1116; --panel:#171c24; --line:#262d38;
    --fg:#e8edf4; --dim:#8b96a5;
    --ok:#3fb950; --warn:#d29922; --hot:#f85149;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    -webkit-text-size-adjust:100%;
    padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }
  .wrap{max-width:520px;margin:0 auto;padding:20px 16px 40px}
  .now{text-align:center;padding:28px 0 8px}
  .val{font-size:clamp(64px,22vw,120px);font-weight:700;line-height:1;
       letter-spacing:-.03em;font-variant-numeric:tabular-nums;transition:color .3s}
  .unit{font-size:.3em;font-weight:600;color:var(--dim);margin-left:.15em}
  .ok{color:var(--ok)} .warn{color:var(--warn)} .hot{color:var(--hot)}
  .stale{color:var(--dim)!important}
  .sub{margin-top:10px;font-size:13px;color:var(--dim)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:var(--ok);margin-right:6px;vertical-align:middle}
  .dot.bad{background:var(--hot)}
  canvas{width:100%;height:140px;display:block;margin:22px 0 6px}
  .axis{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:22px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card .k{font-size:11px;color:var(--dim);letter-spacing:.04em}
  .card .v{font-size:22px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
  .card .v small{font-size:12px;color:var(--dim);font-weight:500;margin-left:3px}
  .wide{grid-column:1 / -1}
  footer{margin-top:24px;text-align:center;font-size:11px;color:var(--dim);line-height:1.7}
</style>
</head>
<body>
<div class="wrap">

  <div class="now">
    <div class="val stale" id="power">----<span class="unit">W</span></div>
    <div class="sub"><span class="dot bad" id="dot"></span><span id="status">接続中...</span></div>
  </div>

  <canvas id="chart" width="1000" height="280"></canvas>
  <div class="axis"><span id="axL">-60分</span><span id="axR">今</span></div>

  <div class="grid">
    <div class="card"><div class="k">電流 R相</div><div class="v" id="cr">--<small>A</small></div></div>
    <div class="card"><div class="k">電流 T相</div><div class="v" id="ct">--<small>A</small></div></div>
    <div class="card wide"><div class="k">積算電力量（正方向）</div><div class="v" id="ef">--<small>kWh</small></div></div>
  </div>

  <footer>
    Cube J1 ローカルモニタ<br>
    <span id="meta">-</span>
  </footer>

</div>

<script>
var HIST = [];
var lastOk = 0;

function fmt(n, d){
  if (n === null || n === undefined || isNaN(n)) return "--";
  return Number(n).toFixed(d === undefined ? 0 : d);
}

function classFor(w){
  if (w >= 3000) return "val hot";
  if (w >= 1200) return "val warn";
  return "val ok";
}

function ago(sec){
  if (sec < 60) return sec + "秒前";
  if (sec < 3600) return Math.floor(sec/60) + "分前";
  return Math.floor(sec/3600) + "時間前";
}

function draw(){
  var c = document.getElementById("chart");
  var ctx = c.getContext("2d");
  var W = c.width, H = c.height;
  ctx.clearRect(0,0,W,H);

  var pts = [];
  for (var i=0;i<HIST.length;i++){
    if (HIST[i][1] !== null && HIST[i][1] !== undefined) pts.push(HIST[i]);
  }
  if (pts.length < 2) return;

  var cutoff = pts[pts.length-1][0] - 3600;
  var win = pts.filter(function(p){ return p[0] >= cutoff; });
  if (win.length < 2) win = pts.slice(-60);

  var vals = win.map(function(p){ return p[1]; });
  var max = Math.max.apply(null, vals);
  var min = Math.min.apply(null, vals);
  if (max - min < 100) { max = min + 100; }
  var pad = (max - min) * 0.15;
  max += pad; min = Math.max(0, min - pad);

  var t0 = win[0][0], t1 = win[win.length-1][0];
  var span = t1 - t0;
  // With too little spread on the time axis (just booted, or several
  // readings inside one second) every point would land on the same x.
  var byTime = span >= 30;
  if (!byTime) span = 1;
  function X(i, t){
    var f = byTime ? (t - t0) / span : (win.length > 1 ? i / (win.length - 1) : 0);
    return f * (W - 4) + 2;
  }
  function Y(v){ return H - 6 - ((v - min) / (max - min)) * (H - 22); }

  // gridlines
  ctx.strokeStyle = "#262d38"; ctx.lineWidth = 2;
  for (var g=0; g<=2; g++){
    var y = 6 + (H - 22) * g / 2;
    ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke();
  }

  // area
  ctx.beginPath();
  ctx.moveTo(X(0, win[0][0]), H);
  for (var j=0;j<win.length;j++) ctx.lineTo(X(j, win[j][0]), Y(win[j][1]));
  ctx.lineTo(X(win.length-1, win[win.length-1][0]), H);
  ctx.closePath();
  var grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, "rgba(63,185,80,.35)");
  grad.addColorStop(1, "rgba(63,185,80,0)");
  ctx.fillStyle = grad; ctx.fill();

  // line
  ctx.beginPath();
  for (var k=0;k<win.length;k++){
    var x = X(k, win[k][0]), y = Y(win[k][1]);
    if (k === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.strokeStyle = "#3fb950"; ctx.lineWidth = 4;
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  ctx.stroke();

  // peak label
  ctx.fillStyle = "#8b96a5";
  ctx.font = "600 22px -apple-system,sans-serif";
  ctx.fillText(Math.round(max - pad) + " W", 8, 26);

  document.getElementById("axL").textContent =
    byTime ? ("-" + Math.round((t1 - t0) / 60) + "分") : "直近";
}

function tick(){
  fetch("/api", {cache:"no-store"}).then(function(r){ return r.json(); }).then(function(d){
    lastOk = Date.now();
    var w = d.power_w;
    var el = document.getElementById("power");
    if (w === null || w === undefined){
      el.innerHTML = "----<span class='unit'>W</span>";
      document.getElementById("status").textContent = "スマートメーターに接続中...";
      return;
    }
    el.className = classFor(w);
    el.innerHTML = fmt(w) + "<span class='unit'>W</span>";
    document.getElementById("cr").innerHTML = fmt(d.current_r_a,1) + "<small>A</small>";
    document.getElementById("ct").innerHTML = fmt(d.current_t_a,1) + "<small>A</small>";
    document.getElementById("ef").innerHTML = fmt(d.energy_forward_kwh,1) + "<small>kWh</small>";
    document.getElementById("dot").className = "dot";
    var age = d.now - d.ts;
    document.getElementById("status").textContent = "更新 " + ago(age);
    document.getElementById("meta").textContent =
      "サンプル " + d.samples + "件 / 稼働 " + Math.floor(d.uptime_s/3600) + "時間";
  }).catch(function(){
    document.getElementById("dot").className = "dot bad";
    document.getElementById("status").textContent = "Cube に接続できません";
    document.getElementById("power").className = "val stale";
  });
}

function loadHistory(){
  fetch("/api/history", {cache:"no-store"}).then(function(r){ return r.json(); })
    .then(function(d){ HIST = d.samples || []; draw(); })
    .catch(function(){});
}

tick(); loadHistory();
setInterval(tick, 5000);
setInterval(loadHistory, 60000);
document.addEventListener("visibilitychange", function(){
  if (!document.hidden){ tick(); loadHistory(); }
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Home-screen icons and web app manifest
#
# Embedded as base64 rather than shipped as files: the Cube only ever gets
# what production_tool copies to /data/local, and one self-contained script
# is easier to keep in step than a directory of assets. Decoded once at
# import, not per request. binascii is used instead of the base64 module
# because mqtt_bridge.py already imports it, so it is known to exist here.
# ---------------------------------------------------------------------------

_ICON_192_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAGw0lEQVR4nOzdS28bVRTA8etH7KTQ"
    "QqokpS5teQgQ3bFD4hvQfoAWsWPBAgkJiW/AjvISCyQWIMECBCwAIRYICTYsYAFCIEpa2jxK0iQQ"
    "8nLeHnuwY9d1fCfOeM61lNzz/63atKvqrzvjc9OT7LHBEQMklTWAAAFBhIAgQkAQISCIEBBECAgi"
    "BAQRAoIIAUGEgCBCQBAhIIgQEEQICCIEBBECgggBQYSAIEJAECEgiBAQRAgIIgQEEQKCCAFBhIAg"
    "QkAQISCIEBBECAgiBAQRAoIIAUGEgCBCQBAhIIgQEEQICCIEBBECgggBQSRtkFTf8VzmSMboRkAJ"
    "hSY8+8IjwXpgdOMRltDI+VMDpwdSJmV04wRKov/0kcKlM6WlklGPE6hrqUzqwZceS2fTpRUCIqDu"
    "nbx4pr8wUP1FaWnbqEdA3bn78WMjFwr1X5eXOYEIqBvpgcwDLz6aSjVenEsEREBdOfP8w32DueZv"
    "CcgQUHyDTw0NPjnU+hUeYYaAYsre23f6uYfavshLtCGgmKqvPpkj7f9WPMIMAcUxfL5w9Nw9bV8M"
    "wzBYJCAC2k996Gx/vbyq/RasjoA6aQ6d7T8qLfICVENAnZy81Bg627jHqCOgPdWGzucLe/1pwE3q"
    "DgKK1jZ0tgWcQDsIKFrb0NnGEKiOgCLYQ2cbQ6A6AmoXOXS2cY9RR0DtIofONh5hdQS0S+TQORKP"
    "sDoCumOvoXMk7jHqCKihw9DZFhSpp4GAGjoMnW28ADURUE3nobONF6AmAtp/6GzjHqOJgPYfOtu4"
    "x2jSHlCcobOttMw7UIPqgGIOnW08wppUBxRz6GwLeIm+TW9A8YfONj7GNykNqKuhs41vR2zSGFBX"
    "Q+dIwQIBNWgMqKuhs417jFbqAup26GzjBaiVroASDJ1tTBFb6QoowdDZxma7VooCSjZ0tjEEaqUl"
    "oMRDZxv3GK20BJR46GzjHqOVioCGLyQfOtuYIrbyP6Da0Pli8qGzLWCtQgvPA5IPnW2cQK1SxwZH"
    "jL8Kz549ceGUOaiqQ+0/X/71UH+s8/kEkg+deyqshONvXj3sQwFvA3IydO6pmU9vrl5ZMYectwE5"
    "GTr3zspvS3NfTJvDz8+AXA2de2R7YWviravGCx4G5HDo3AthUBm7PFpeLxsveBiQw6FzL0x9ML4x"
    "vmZ84VtAbofOzi3+OD//7ZzxiFcBOR86u7U5vT75znXjF38C6sXQ2aHKVvnGq6PhdsX4xZ+ACs+I"
    "vtO516pnz/bcpvGOJwFVh87DTx/cofM/38ws/fSf8ZEPAR3wofP62Or0hxPGUz4EdJCHzsFaUJ36"
    "mHJoPHXoAzrIQ+cwDKsTZ79/LMvhDuiAD51nP58q/r5svOb59wPFlL9/4NxrTxinileWr7/yh/H2"
    "2dXAhrKa/Il+41RpaXv8jave12MIqC434jKgsByOvT6q5EcaElCN2xNo+qOJ9b9WjQ4EVNPvLqDl"
    "nxf+/XrGqEFANTlHAW3Nbky8fc1oQkA1+fscBFQpVarXpZUt365LOyMg0zeUS6UdXIPcfPf61q0N"
    "owwBmbyLj2Dz388t/jBv9CEgBy9AG5NrU++NGZUISPoCVN4s37g8GgYKhoZRCMjkhkUBVT92lea3"
    "jFYEJDqB5r6aXvll0ShGQKY/aUBr14q3Pp40umkPKHNXJuGPyyiWqhdeRtfQJ4L2gJLdgvmxWMMJ"
    "7QEl+ww/84kPizWcUH8CdT9FrC3W+NKHxRpOqD+BunyD9mmxhhO8A3URkGeLNZxQfwKN5OP/Zc8W"
    "azihO6CUyQ/FPYH8W6zhhOqA8ifj1uPlYg0nVAcU8zO8r4s1nFAdUH+8z/C+LtZwghNoHx4v1nBC"
    "9zvQfkMgvxdrOKH7BOr4CPN+sYYTuk+gvQPSsFjDCb0BZY/3pXN7LlTUsFjDCb0BdTh+ileWZz/7"
    "2yAGxQHt8RFMz2INJ/QGFHkPr2qxhhOKT6Co/4yharGGEzzC7tC2WMMJxQHtfoQpXKzhhNKA0gOZ"
    "7NG+5m91LtZwQmlA+RO7vo9M52INJ5QG1HqNOv+d0sUaTqg9gRoB1RZrvK90sYYTqgNSvljDCa2P"
    "sJ17DOWLNZzQewKxWMMJjQGFKVNa3GaxhhMaf1ZG5mg2lUkFS6xGcEDjCVQuclfqDAumIEJAECEg"
    "iBAQRAgIIgQEEQKCCAFBhIAgQkAQISCIEBBECAgiBAQRAoIIAUGEgCBCQBAhIIgQEEQICCIEBBEC"
    "gggBQYSAIEJAECEgiBAQRAgIIgQEEQKCCAFBhIAgQkAQISCIEBBECAgiBAQRAoIIAUGEgCBCQBD5"
    "HwAA///iTpVHAAAABklEQVQDAN5/LIj1FuoVAAAAAElFTkSuQmCC"
)

_ICON_512_B64 = (
    "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"
    "m/pq1Xv+sbNPT7npBdZDAIjOSrYATT97/eIX3fQC6yIARGfwXgFYuDQ/dXQiAOsjAETn7kMAS42l"
    "+uMnmrOGvmC9BIDo3P0VkJteoFMEgOjc5RvQ9k0v/3MhAJ0gAETnTq+AFs7NuekFOkgAiEulv1Lb"
    "ucwTQHO+8eITJ9z0Ah0kAMTlTgcA7ZtezswFoHMEgLgs+/7n0n+fd9MLdJwAEJdiAGanZk7/fT0A"
    "nSYAxOUNr4DaN70cOeGmF+gGASAug6+/CWDy6MTiZZe8Q1cIAHH58SeA819w0wt0kQthiMsPzwBu"
    "Tdw4+/RUALrGEwAR6d89UKm2b3dp3Fx00wt0mwAQkYE97RGw9k0vT7npBbpOAIjIq+9/zv3LSzPH"
    "bwSgy5wBEJGXT4Cnn73+8tlvALrPEwAR6evvmzo6UQle/UMvVLbdPxwgDtWhanPG0Bf0iCcAIuJv"
    "f+glAQDIlAAAZEoAADIlAACZEgCATAkAQKYEACBTAgCQKQEAyJQAAGRKAAAyJQAAmRIAgEwJAECm"
    "BAAgUwIAkCkBAMiUAABkSgAAMiUAAJkSAIBMCQBApgQAIFMCAJApAQDIlAAAZEoAADIlAACZEgCA"
    "TAkAQKYEACBTAgCQKQEAyJQAAGRKAAAyJQAAmRIAgEwJAECmBAAgUwIAkCkBAMiUAABkSgAAMiUA"
    "AJkSAIBMCQBApgQAIFMCAJApAQDIlAAAZEoAADIlAACZEgCATAkAQKYEACBTAgCQKQEAyJQAAGRK"
    "AAAyJQAAmRIAgEwJAECmBAAgUwIAkCkBAMiUAABkSgAAMiUAAJkSAIBMCQBApgQAIFMCAJApAQDI"
    "lAAAZEoAADIlAACZEgCATAkAQKYEACBTAgCQKQEAyJQAAGRKAAAyJQAAmRIAgEwJAECmBAAgUwIA"
    "kCkBAMiUAABkSgAAMiUAAJkSAIBMCQBApgQAIFMCAJApAQDIlAAAZEoAADIlAACZEgCATAkAQKYE"
    "ACBTAgCQKQEAyJQAAGRKAAAyJQAAmRIAgEwJAECmBAAgUwIAkCkBAMiUAABk6v8BAAD//4fpo2wA"
    "AAAGSURBVAMA1e/mHvd93FEAAAAASUVORK5CYII="
)

_ICON_180_B64 = (
    "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"
)

ICON_192 = binascii.a2b_base64(_ICON_192_B64)
ICON_512 = binascii.a2b_base64(_ICON_512_B64)
ICON_180 = binascii.a2b_base64(_ICON_180_B64)

# start_url and the icon paths are root-relative on purpose: they resolve
# against whatever address the phone used to reach the Cube, so nothing here
# has to know the device's IP.
MANIFEST = """{
  "name": "電力モニタ",
  "short_name": "電力モニタ",
  "description": "Cube J1 から読む自宅の瞬時消費電力",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0e1116",
  "theme_color": "#0e1116",
  "icons": [
    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
  ]
}
"""

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#0e1116"/>
<path d="M18.5 3 L8 18 h6 l-1.5 11 L24 14 h-6.5 z" fill="#3fb950"/>
</svg>
"""

if __name__ == "__main__":
    main()
