#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Cube J1 local power monitor: this repository's fork of
# tsuyopon123/cube-j1-mqtt at 3bbb6bf. Differences from upstream, each found
# on a real meter:
#   - LED writes go to /dev/null (upstream blinked through every command)
#   - the scan deadline follows the real SKSCAN sweep time; duration capped at 8
#   - five consecutive ERXUDP timeouts force a re-join
#   - replies are matched to their request by TID and source object
#   - 30-minute cumulative history (EPC 0xE2) is fetched and published per day
"""
mqtt_bridge.py  -  Wi-SUN B-route -> ECHONET Lite -> Home Assistant MQTT
Python 2.7 stdlib only: termios, fcntl, select, socket, struct, json, os
"""

from __future__ import print_function

import os
import sys
import json
import time
import struct
import socket
import select
import binascii
import termios
import fcntl
import collections
import re
import threading

CONFIG_PATH = "/data/local/config.json"
LOG_PATH    = "/data/local/mqtt_bridge.log"

LED_R = "/dev/null"
LED_G = "/dev/null"
LED_B = "/dev/null"

def led_rgb(r, g, b):
    for path, val in ((LED_R, r), (LED_G, g), (LED_B, b)):
        try:
            with open(path, 'w') as f:
                f.write(str(val) + '\n')
        except Exception:
            pass

def led_read():
    result = []
    for path in (LED_R, LED_G, LED_B):
        try:
            with open(path) as f:
                result.append(int(f.read().strip()))
        except Exception:
            result.append(0)
    return tuple(result)

_log_file = None

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}\n".format(ts, msg)
    global _log_file
    if _log_file:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass
    else:
        sys.stderr.write(line)
        sys.stderr.flush()

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Serial port (termios, no pyserial)
# ---------------------------------------------------------------------------

def open_serial(port, baud=115200):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)

    attrs = list(termios.tcgetattr(fd))
    iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]

    # raw input
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
               termios.ISTRIP | termios.INLCR  | termios.IGNCR  |
               termios.ICRNL  | termios.IXON)
    oflag &= ~termios.OPOST
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |=  termios.CS8 | termios.CREAD | termios.CLOCAL
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
               termios.ISIG | termios.IEXTEN)

    baud_map = {
        9600:   termios.B9600,
        19200:  termios.B19200,
        38400:  termios.B38400,
        57600:  termios.B57600,
        115200: termios.B115200,
    }
    baud_const = baud_map.get(baud, termios.B115200)

    cc = attrs[6]
    # attrs[6] must be returned in the same type tcgetattr gave us.
    # On this device Python 2.7 it is a list of 32 ints; tcsetattr rejects bytes.
    if isinstance(cc, list):
        cc_list = list(cc)
        cc_list[termios.VMIN]  = 1
        cc_list[termios.VTIME] = 0
        attrs[6] = cc_list
    else:
        # bytes/bytearray path
        cc_arr = bytearray(cc)
        cc_arr[termios.VMIN]  = 1
        cc_arr[termios.VTIME] = 0
        attrs[6] = bytes(cc_arr)

    attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
    attrs[4] = baud_const
    attrs[5] = baud_const

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd

def serial_write(fd, data):
    if isinstance(data, bytes):
        os.write(fd, data)
    else:
        os.write(fd, data.encode("ascii"))

def serial_readline(fd, timeout=10):
    """Read one CRLF-terminated line; return decoded str or None on timeout."""
    buf = b""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not r:
            continue
        ch = os.read(fd, 1)
        if not ch:
            continue
        buf += ch
        if buf.endswith(b"\r\n"):
            return buf[:-2].decode("ascii", errors="replace")
    return buf.decode("ascii", errors="replace") if buf else None

def _led_blink(stop_event, colors, interval=0.2):
    i = 0
    while not stop_event.is_set():
        led_rgb(*colors[i % len(colors)])
        i += 1
        stop_event.wait(interval)

def skcommand(fd, cmd, timeout=10):
    """Send one SKSTACK command; return list of response lines (up to OK/FAIL)."""
    orig_led = led_read()
    stop_event = threading.Event()
    t = threading.Thread(target=_led_blink,
                         args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
    t.daemon = True
    t.start()

    serial_write(fd, cmd + "\r\n")
    lines = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
            if line is None:
                break
            lines.append(line)
            if line in ("OK", ) or line.startswith("FAIL"):
                break
    finally:
        stop_event.set()
        t.join(timeout=1)
        led_rgb(*orig_led)
    return lines

# ---------------------------------------------------------------------------
# Scan settings
# ---------------------------------------------------------------------------

SCAN_DURATION_BASE = 4
SCAN_RETRY_LIMIT = 8

# ---------------------------------------------------------------------------
# SKSTACK-IP / Wi-SUN B-route connection
# ---------------------------------------------------------------------------

def skscan(fd):
    """Active scan with retries; returns best PAN info dict or empty dict."""
    duration = SCAN_DURATION_BASE
    
    while duration <= SCAN_RETRY_LIMIT:
        # Clear stale lines from previous command/scan cycle.
        termios.tcflush(fd, termios.TCIFLUSH)

        log("SKSCAN try duration={}".format(duration))
        # BP35C0 style scan command: <mode> <channel_mask> <duration> <side>
        serial_write(fd, "SKSCAN 2 FFFFFFFF {} 0\r\n".format(duration))

        pan_list  = []
        current   = {}
        scan_done = False
        deadline  = time.time() + 0.01 * (2 ** duration + 1) * 32 + 10
        while time.time() < deadline:
            line = serial_readline(fd, timeout=2)
            if line is None:
                continue
            if line.startswith("EVENT 20"):
                if current:
                    pan_list.append(current)
                current = {}
            elif line.startswith("EVENT 22"):
                if current:
                    pan_list.append(current)
                scan_done = True
                break  # Exit loop once EVENT 22 received
            elif ":" in line and not line.startswith("EVENT"):
                key, _, val = line.strip().partition(":")
                current[key.strip()] = val.strip()

        if pan_list:
            log("SKSCAN found {} PAN(s), selecting best LQI".format(len(pan_list)))
            pan_list.sort(key=lambda p: int(p.get("LQI", "0"), 16), reverse=True)
            return pan_list[0]

        log("SKSCAN no PAN found, retrying with longer duration")
        duration += 1

    return {}

def skll64(fd, mac):
    """Convert MAC address to IPv6 link-local address.

    Reads lines until an IPv6-like substring (hex digits + colons) is found
    and validated. Returns the candidate string or None on timeout.
    """
    serial_write(fd, "SKLL64 {}\r\n".format(mac))
    deadline = time.time() + 10
    while time.time() < deadline:
        line = serial_readline(fd, timeout=2)
        if not line:
            continue
        # skip echoes and obvious non-data lines
        if line.startswith("SKLL64") or line.strip() == "":
            continue
        # extract only hex+colon runs (length threshold to avoid short noise)
        m = re.search(r'([0-9A-Fa-f:]{15,})', line)
        if not m:
            continue
        candidate = m.group(1)
        # validate with inet_pton if available
        try:
            socket.inet_pton(socket.AF_INET6, candidate)
            return candidate
        except Exception:
            # not valid IPv6; continue waiting for a proper response
            log("skll64: received candidate but validation failed: {}".format(candidate))
            continue
    return None

def wisun_connect(fd, br_id, br_pwd):
    """Full SKSTACK-IP join sequence. Returns IPv6 address of meter."""
    log("SKRESET")
    skcommand(fd, "SKRESET", timeout=5)
    time.sleep(1)

    log("SKSETPWD")
    skcommand(fd, "SKSETPWD C {}".format(br_pwd))

    log("SKSETRBID")
    skcommand(fd, "SKSETRBID {}".format(br_id))

    # Force ASCII-hex ERXUDP payload format so parser stays stable.
    skcommand(fd, "WOPT 1")

    log("SKSCAN (may take up to 60s)")
    pan = skscan(fd)
    if not pan.get("Channel") or not pan.get("Pan ID") or not pan.get("Addr"):
        raise RuntimeError("SKSCAN: no PAN found ({})".format(pan))

    channel = pan["Channel"]
    pan_id  = pan["Pan ID"]
    mac     = pan["Addr"]
    log("PAN found: ch={} panId={} mac={}".format(channel, pan_id, mac))

    ipv6 = skll64(fd, mac)
    if not ipv6:
        raise RuntimeError("SKLL64 failed")
    log("Meter IPv6: {}".format(ipv6))

    skcommand(fd, "SKSREG S2 {}".format(channel))
    skcommand(fd, "SKSREG S3 {}".format(pan_id))

    log("SKJOIN {}".format(ipv6))
    serial_write(fd, "SKJOIN {}\r\n".format(ipv6))

    orig_led = led_read()
    stop_event = threading.Event()
    t = threading.Thread(target=_led_blink,
                         args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
    t.daemon = True
    t.start()
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            line = serial_readline(fd, timeout=2)
            if line is None:
                continue
            if "EVENT 25" in line:
                log("SKJOIN: connected")
                return ipv6
            if "EVENT 24" in line:
                raise RuntimeError("SKJOIN: PANA authentication failed (EVENT 24)")
    finally:
        stop_event.set()
        t.join(timeout=1)
        led_rgb(*orig_led)

    raise RuntimeError("SKJOIN: timeout")

# ---------------------------------------------------------------------------
# ECHONET Lite frame builder / parser
# ---------------------------------------------------------------------------

EPCS = [0xD3, 0xE1, 0xE7, 0xE0, 0xE3, 0xE8]

def build_el_get(tid, epcs):
    frame = bytearray()
    frame += b"\x10\x81"                     # EHD1, EHD2
    frame += struct.pack(">H", tid & 0xFFFF) # TID
    frame += b"\x05\xFF\x01"                 # SEOJ: controller
    frame += b"\x02\x88\x01"                 # DEOJ: smart meter
    frame += b"\x62"                         # ESV: Get
    frame += struct.pack("B", len(epcs))     # OPC
    for epc in epcs:
        frame += struct.pack("BB", epc, 0)   # EPC, PDC=0
    return bytes(frame)

def parse_el_response(data):
    """Returns dict {epc_int: bytearray}."""
    if len(data) < 12:
        return {}
    esv = data[10] if isinstance(data[10], int) else ord(data[10])
    opc = data[11] if isinstance(data[11], int) else ord(data[11])
    # Accept Get_Res (0x72) or Get_SNA (0x52)
    if esv not in (0x72, 0x52):
        return {}
    result = {}
    pos = 12
    for _ in range(opc):
        if pos + 2 > len(data):
            break
        epc = data[pos] if isinstance(data[pos], int) else ord(data[pos])
        pdc = data[pos+1] if isinstance(data[pos+1], int) else ord(data[pos+1])
        pos += 2
        if pos + pdc > len(data):
            break
        result[epc] = bytearray(data[pos:pos+pdc])
        pos += pdc
    return result

def decode_measurements(props):
    result = {}

    # D3: coefficient (4-byte unsigned)
    if 0xD3 in props and len(props[0xD3]) >= 4:
        result["coefficient"] = struct.unpack(">I", bytes(props[0xD3][:4]))[0]

    # E1: unit exponent byte
    if 0xE1 in props and len(props[0xE1]) >= 1:
        unit_byte = props[0xE1][0]
        unit_map = {0x00: 1.0, 0x01: 0.1,  0x02: 0.01,   0x03: 0.001, 0x04: 0.0001,
                    0x0A: 10.0, 0x0B: 100.0, 0x0C: 1000.0, 0x0D: 10000.0}
        result["unit_kwh"] = unit_map.get(unit_byte, 1.0)

    # E7: instantaneous power W (4-byte signed)
    if 0xE7 in props and len(props[0xE7]) >= 4:
        result["power_w"] = struct.unpack(">i", bytes(props[0xE7][:4]))[0]

    # E0: cumulative forward kWh (4-byte unsigned × coeff × unit)
    if 0xE0 in props and len(props[0xE0]) >= 4:
        result["energy_forward_raw"] = struct.unpack(">I", bytes(props[0xE0][:4]))[0]

    # E3: cumulative reverse kWh (4-byte unsigned × coeff × unit)
    if 0xE3 in props and len(props[0xE3]) >= 4:
        result["energy_reverse_raw"] = struct.unpack(">I", bytes(props[0xE3][:4]))[0]

    # E8: instantaneous current R,T phase (2×signed short, 0.1A)
    if 0xE8 in props and len(props[0xE8]) >= 4:
        r, t = struct.unpack(">hh", bytes(props[0xE8][:4]))
        result["current_r_a"] = r / 10.0
        result["current_t_a"] = t / 10.0

    return result

def apply_energy_scale(measurements, coeff, unit_kwh):
    c = measurements.get("coefficient", coeff)
    u = measurements.get("unit_kwh", unit_kwh)
    if "energy_forward_raw" in measurements:
        measurements["energy_forward_kwh"] = measurements["energy_forward_raw"] * c * u
    if "energy_reverse_raw" in measurements:
        measurements["energy_reverse_kwh"] = measurements["energy_reverse_raw"] * c * u
    return measurements

# ---------------------------------------------------------------------------
# Send ECHONET Lite Get via SKSENDTO
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Generic ECHONET Lite request / reply, matched by TID
# ---------------------------------------------------------------------------

JST_OFFSET       = 9 * 3600    # the meter keeps Japan time; the Cube clock is UTC
HISTORY_DAYS     = 9           # 0xE2 answers for today plus 8 days on this meter
HISTORY_NODATA   = 0xFFFFFFFE  # slot not recorded (yet)
HISTORY_PER_LOOP = 3           # days fetched per poll cycle, so a mute meter
                               # cannot stall the live readings for minutes

def build_el_frame(tid, esv, items):
    """items: list of (epc, data) where data is bytes or a list of ints."""
    frame = bytearray()
    frame += b"\x10\x81"
    frame += struct.pack(">H", tid & 0xFFFF)
    frame += b"\x05\xFF\x01"
    frame += b"\x02\x88\x01"
    frame += struct.pack("B", esv)
    frame += struct.pack("B", len(items))
    for epc, data in items:
        data = bytearray(data)
        frame += struct.pack("BB", epc, len(data))
        frame += data
    return bytes(frame)

def send_el(fd, ipv6, tid, esv, items):
    frame = build_el_frame(tid, esv, items)
    cmd = "SKSENDTO 1 {} 0E1A 1 0 {:04X} ".format(ipv6, len(frame))
    serial_write(fd, cmd)
    serial_write(fd, frame)
    serial_write(fd, b"\r\n")

def _byte(data, i):
    return data[i] if isinstance(data[i], int) else ord(data[i])

def reply_matches(data, tid):
    """True when `data` is the meter's answer to request `tid`."""
    if data is None or len(data) < 12:
        return False
    rtid = (_byte(data, 2) << 8) | _byte(data, 3)
    seoj = (_byte(data, 4), _byte(data, 5), _byte(data, 6))
    return rtid == (tid & 0xFFFF) and seoj == (0x02, 0x88, 0x01)

def read_el_reply(fd, tid, timeout=15):
    """Wait for the answer to request `tid`.

    Upstream took the first ECHONET frame it saw. The meter also sends
    unsolicited notifications (ESV 0x73 from the node profile), and a late
    answer to an earlier request can still be in flight; both used to be
    mistaken for the reply, which is where the stray "Measurements: {}"
    lines came from. Anything that does not match is skipped."""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        data = read_erxudp(fd, timeout=remaining)
        if data is None:
            return None
        if reply_matches(data, tid):
            return data

def parse_el_props(data):
    """(esv, {epc: bytearray}) for any ESV."""
    esv = _byte(data, 10)
    opc = _byte(data, 11)
    out = {}
    pos = 12
    for _ in range(opc):
        if pos + 2 > len(data):
            break
        epc = _byte(data, pos)
        pdc = _byte(data, pos + 1)
        pos += 2
        if pos + pdc > len(data):
            break
        out[epc] = bytearray(data[pos:pos + pdc])
        pos += pdc
    return esv, out

def decode_history_day(b, day):
    """48 cumulative raw readings from an 0xE2 payload, None for empty slots.
    Returns None if the payload is short or belongs to a different day."""
    if b is None or len(b) < 2 + 48 * 4:
        return None
    if struct.unpack(">H", bytes(b[0:2]))[0] != day:
        return None
    vals = []
    for i in range(48):
        v = struct.unpack(">I", bytes(b[2 + i * 4:6 + i * 4]))[0]
        vals.append(None if v >= HISTORY_NODATA else v)
    return vals

def fetch_history_day(fd, ipv6, tid, day):
    """Readings for `day` days ago (0 = today, Japan time).
    0xE5 selects the day (SetC), then 0xE2 returns its 48 half-hour slots.
    Returns (next_tid, list of 48) or (next_tid, None)."""
    send_el(fd, ipv6, tid, 0x61, [(0xE5, [day])])
    reply = read_el_reply(fd, tid, 15)
    tid = (tid + 1) & 0xFFFF
    if reply is None or parse_el_props(reply)[0] != 0x71:
        return tid, None
    send_el(fd, ipv6, tid, 0x62, [(0xE2, [])])
    reply = read_el_reply(fd, tid, 20)
    tid = (tid + 1) & 0xFFFF
    if reply is None:
        return tid, None
    esv, props = parse_el_props(reply)
    if esv != 0x72:
        return tid, None
    return tid, decode_history_day(props.get(0xE2), day)

def jst_date(ts, days_ago=0):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + JST_OFFSET - days_ago * 86400))

HISTORY_SETTLE = 90   # s after a half-hour boundary before the meter has the new record

def settled(ts):
    """True once the meter has had time to write the half-hour just ended."""
    return (int(ts) + JST_OFFSET) % 1800 > HISTORY_SETTLE

def queue_history(now, hist_date, hist_slot, pending_days):
    """Add the meter days that need fetching now to pending_days, and return
    the (hist_date, hist_slot) to remember once nothing is pending.

    A half-hour only counts as handled after it has settled. Marking it any
    earlier skips the refresh for good: polls are about a minute apart, so the
    first one after a boundary nearly always falls inside the settle window."""
    today = jst_date(now)
    slot = int((now + JST_OFFSET) // 1800)
    if hist_date is not None and today != hist_date:
        pending_days.update([0, 1])          # yesterday is complete now
    elif slot != hist_slot and settled(now):
        pending_days.add(0)                  # a new half-hour was recorded
    if settled(now):
        return today, slot
    return hist_date, hist_slot

def near_jst_midnight(ts, margin=120):
    """The meter rolls its day index at Japan midnight. Stay clear of it so a
    fetch is never attributed to the wrong date."""
    sec = (int(ts) + JST_OFFSET) % 86400
    return sec < margin or sec > 86400 - margin

def publish_history_day(mqtt, device_id, date, raw, coeff, unit_kwh):
    kwh = [None if v is None else round(v * coeff * unit_kwh, 4) for v in raw]
    mqtt.publish("cubej/{}/history_day".format(device_id), {"date": date, "kwh": kwh})

def send_el_get(fd, ipv6, tid):
    frame = build_el_get(tid, EPCS)
    # SKSENDTO expects 4-hex-digit payload length and trailing CRLF after raw data.
    cmd = "SKSENDTO 1 {} 0E1A 1 0 {:04X} ".format(ipv6, len(frame))
    serial_write(fd, cmd)
    serial_write(fd, frame)
    serial_write(fd, b"\r\n")

def read_erxudp(fd, timeout=15):
    """Wait for ERXUDP and return payload as bytearray, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
        if line is None:
            continue
        if line.startswith("ERXUDP"):
            parts = line.split()
            # Tail fields are stable: ... <secured> <side> <datalen> <data>
            if len(parts) >= 10:
                hex_data = parts[-1].strip()
                if not hex_data.startswith("1081"):
                    continue
                try:
                    return bytearray(binascii.unhexlify(hex_data))
                except Exception as e:
                    log("ERXUDP hex decode error: {}".format(e))
    return None

# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (raw socket, no paho)
# ---------------------------------------------------------------------------

def _encode_remaining(n):
    buf = b""
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        buf += struct.pack("B", byte)
        if n == 0:
            break
    return buf

def _encode_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b

class MQTTClient(object):
    def __init__(self, host, port, client_id, username=None, password=None):
        self.host      = host
        self.port      = port
        self.client_id = client_id
        self.username  = username
        self.password  = password
        self.sock      = None
        self._out_queue = collections.deque()

    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        # Enable TCP keepalive where available
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # platform-specific options
            for opt_name, opt_val in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10), ('TCP_KEEPCNT', 3)):
                if hasattr(socket, opt_name):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt_name), opt_val)
                    except Exception:
                        pass
        except Exception:
            pass

        s.connect((self.host, self.port))

        flags = 0x02  # clean session
        if self.username: flags |= 0x80
        if self.password: flags |= 0x40

        var_hdr = (b"\x00\x04MQTT"
                   + b"\x04"
                   + struct.pack("B", flags)
                   + b"\x00\x3C")   # keep-alive 60s

        payload = _encode_str(self.client_id)
        if self.username: payload += _encode_str(self.username)
        if self.password: payload += _encode_str(self.password)

        remaining = var_hdr + payload
        pkt = b"\x10" + _encode_remaining(len(remaining)) + remaining
        s.sendall(pkt)

        # read CONNACK
        s.settimeout(10)
        ack = b""
        while len(ack) < 4:
            chunk = s.recv(4 - len(ack))
            if not chunk:
                break
            ack += chunk
        s.settimeout(None)

        if len(ack) < 4 or (ack[0] if isinstance(ack[0], int) else ord(ack[0])) != 0x20:
            raise RuntimeError("MQTT: bad CONNACK ({})".format(binascii.hexlify(ack)))
        rc = ack[3] if isinstance(ack[3], int) else ord(ack[3])
        if rc != 0:
            raise RuntimeError("MQTT: connection refused code {}".format(rc))

        self.sock = s
        log("MQTT connected to {}:{}".format(self.host, self.port))

        # flush any queued messages
        try:
            self._flush_queue()
        except Exception as e:
            log("MQTT flush queue error: {}".format(e))

    def _make_pkt(self, topic, payload, retain=False):
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        topic_b = topic.encode("utf-8")
        payload_b = payload.encode("utf-8") if isinstance(payload, str) else payload
        fixed = 0x30 | (0x01 if retain else 0x00)
        var_hdr = struct.pack(">H", len(topic_b)) + topic_b
        remaining = var_hdr + payload_b
        return struct.pack("B", fixed) + _encode_remaining(len(remaining)) + remaining

    def publish(self, topic, payload, retain=False):
        pkt = self._make_pkt(topic, payload, retain)
        try:
            if not self.sock:
                raise RuntimeError("No MQTT socket")
            self.sock.sendall(pkt)
            return
        except Exception as e:
            log("MQTT publish error: {}".format(e))
            # try reconnect and resend
            try:
                self._reconnect()
            except Exception as e2:
                log("MQTT reconnect failed after publish error: {}".format(e2))
                # queue the message for later delivery
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass
                return

            try:
                self.sock.sendall(pkt)
                return
            except Exception as e3:
                log("MQTT publish retry failed: {}".format(e3))
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass

    def _flush_queue(self):
        while self._out_queue and self.sock:
            topic, payload, retain = self._out_queue[0]
            try:
                pkt = self._make_pkt(topic, payload, retain)
                self.sock.sendall(pkt)
                self._out_queue.popleft()
            except Exception as e:
                log("MQTT queued publish failed: {}".format(e))
                break

    def ping(self):
        try:
            self.sock.sendall(b"\xC0\x00")
        except Exception as e:
            log("MQTT ping error: {}".format(e))
            self._reconnect()
            return
        # wait for PINGRESP (should be 0xD0 0x00)
        try:
            r, _, _ = select.select([self.sock], [], [], 5)
            if r:
                resp = self.sock.recv(2)
                if not resp:
                    log("MQTT ping: no response (empty)")
                    self._reconnect()
                elif len(resp) < 2:
                    log("MQTT ping: incomplete response (len={})".format(len(resp)))
                    self._reconnect()
                else:
                    first_byte = resp[0] if isinstance(resp[0], int) else ord(resp[0])
                    if first_byte != 0xD0:
                        log("MQTT ping: unexpected response first_byte=0x{:02X}".format(first_byte))
                        self._reconnect()
            else:
                log("MQTT ping: timeout (no data within 5s)")
                self._reconnect()
        except Exception as e:
            log("MQTT ping recv error: {}".format(e))
            self._reconnect()

    def _reconnect(self):
        log("MQTT reconnecting …")
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        while True:
            try:
                self.connect()
                return
            except Exception as e:
                log("MQTT reconnect failed: {} - retry in 15s".format(e))
                time.sleep(15)

# ---------------------------------------------------------------------------
# Home Assistant MQTT auto-discovery
# ---------------------------------------------------------------------------

SENSOR_DEFS = [
    ("power",          "Instantaneous Power",  "W",   "power",   "measurement"),
    ("energy_forward", "Cumulative Energy Fwd", "kWh", "energy",  "total_increasing"),
    ("energy_reverse", "Cumulative Energy Rev", "kWh", "energy",  "total_increasing"),
    ("current_r",      "Current R Phase",       "A",   "current", "measurement"),
    ("current_t",      "Current T Phase",       "A",   "current", "measurement"),
]

def publish_ha_discovery(mqtt, device_id):
    device = {
        "identifiers": [device_id],
        "name":         "Cube J1 Smart Meter",
        "model":        "Cube J1",
        "manufacturer": "NextDrive",
    }
    base = "cubej/{}".format(device_id)
    for sid, name, unit, dev_class, state_class in SENSOR_DEFS:
        topic  = "homeassistant/sensor/{}/{}/config".format(device_id, sid)
        config = {
            "name":               name,
            "unique_id":          "{}_{}".format(device_id, sid),
            "state_topic":        "{}/{}".format(base, sid),
            "unit_of_measurement": unit,
            "device_class":       dev_class,
            "state_class":        state_class,
            "device":             device,
        }
        mqtt.publish(topic, config, retain=True)
        log("HA discovery: {}".format(topic))

def publish_measurements(mqtt, device_id, m):
    base = "cubej/{}".format(device_id)
    if "power_w" in m:
        mqtt.publish("{}/power".format(base), str(m["power_w"]))
    if "energy_forward_kwh" in m:
        mqtt.publish("{}/energy_forward".format(base), "{:.3f}".format(m["energy_forward_kwh"]))
    if "energy_reverse_kwh" in m:
        mqtt.publish("{}/energy_reverse".format(base), "{:.3f}".format(m["energy_reverse_kwh"]))
    if "current_r_a" in m:
        mqtt.publish("{}/current_r".format(base), "{:.1f}".format(m["current_r_a"]))
    if "current_t_a" in m:
        mqtt.publish("{}/current_t".format(base), "{:.1f}".format(m["current_t_a"]))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _log_file
    try:
        _log_file = open(LOG_PATH, "a")
    except Exception:
        pass

    cfg           = load_config()
    br_id         = cfg["br_id"]
    br_pwd        = cfg["br_pwd"]
    ha_host       = cfg["mqtt_host"]
    ha_port       = int(cfg.get("mqtt_port", 1883))
    ha_user       = cfg.get("mqtt_user", "")
    ha_pass       = cfg.get("mqtt_pass", "")
    device_id     = cfg.get("device_id", "cubej1")
    serial_port   = cfg.get("serial_port", "/dev/ttyS1")
    poll_interval = int(cfg.get("poll_interval", 60))

    log("=== mqtt_bridge start device_id={} ===".format(device_id))

    # Connect MQTT
    mqtt = MQTTClient(ha_host, ha_port, "cubej1_{}".format(device_id),
                      username=ha_user, password=ha_pass)
    while True:
        try:
            mqtt.connect()
            break
        except Exception as e:
            log("MQTT connect failed: {} - retry in 15s".format(e))
            time.sleep(15)

    publish_ha_discovery(mqtt, device_id)

    # Open serial port
    log("Opening serial {}".format(serial_port))
    fd = None
    while True:
        try:
            fd = open_serial(serial_port)
            break
        except Exception as e:
            log("Serial open failed: {} - retry in 10s".format(e))
            time.sleep(10)

    # Wi-SUN join
    ipv6 = None
    while True:
        try:
            ipv6 = wisun_connect(fd, br_id, br_pwd)
            break
        except Exception as e:
            log("Wi-SUN join failed: {} - retry in 60s".format(e))
            time.sleep(60)

    log("Meter connected at {}".format(ipv6))

    tid       = 1
    coeff     = 1
    unit_kwh  = 1.0
    last_ping = time.time()
    misses    = 0
    unit_seen = False
    pending_days = set(range(HISTORY_DAYS))
    hist_date = None
    hist_slot = None

    while True:
        try:
            orig_led = led_read()
            led_rgb(0, 0, 255)
            try:
                send_el(fd, ipv6, tid, 0x62, [(e, []) for e in EPCS])
                data = read_el_reply(fd, tid, 15)
                tid = (tid + 1) & 0xFFFF
                if data:
                    misses = 0
                    props = parse_el_response(data)
                    m     = decode_measurements(props)
                    m     = apply_energy_scale(m, coeff, unit_kwh)
                    if "coefficient" in m:
                        coeff = m["coefficient"]
                    if "unit_kwh" in m:
                        unit_kwh = m["unit_kwh"]
                        unit_seen = True
                    log("Measurements: {}".format(
                        {k: v for k, v in m.items()
                         if k in ("power_w", "energy_forward_kwh", "energy_reverse_kwh",
                                   "current_r_a", "current_t_a")}))
                    publish_measurements(mqtt, device_id, m)
                else:
                    misses += 1
                    log("No ERXUDP response (timeout) [{}/5]".format(misses))
                    if misses >= 5:
                        misses = 0
                        raise RuntimeError("5 consecutive ERXUDP timeouts")
            finally:
                led_rgb(*orig_led)

            # Half-hour history. Needs the energy unit first, which arrives
            # with the first successful reading.
            now = time.time()
            if unit_seen and not near_jst_midnight(now):
                mark = queue_history(now, hist_date, hist_slot, pending_days)
                for day in sorted(pending_days, reverse=True)[:HISTORY_PER_LOOP]:
                    tid, raw = fetch_history_day(fd, ipv6, tid, day)
                    if raw is None:
                        log("History day -{}: no answer".format(day))
                        continue
                    date = jst_date(now, day)
                    publish_history_day(mqtt, device_id, date, raw, coeff, unit_kwh)
                    log("History {} ({}/48 slots)".format(
                        date, len([v for v in raw if v is not None])))
                    pending_days.discard(day)
                if not pending_days:
                    hist_date, hist_slot = mark

            if time.time() - last_ping > 50:
                mqtt.ping()
                last_ping = time.time()

            time.sleep(poll_interval)

        except Exception as e:
            log("Main loop error: {} - reconnecting Wi-SUN in 30s".format(e))
            time.sleep(30)
            try:
                ipv6 = wisun_connect(fd, br_id, br_pwd)
                log("Wi-SUN reconnected at {}".format(ipv6))
            except Exception as e2:
                log("Wi-SUN reconnect failed: {}".format(e2))


if __name__ == "__main__":
    main()
