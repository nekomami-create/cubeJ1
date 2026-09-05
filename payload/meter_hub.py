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

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#0e1116"/>
<path d="M18.5 3 L8 18 h6 l-1.5 11 L24 14 h-6.5 z" fill="#3fb950"/>
</svg>
"""

if __name__ == "__main__":
    main()
