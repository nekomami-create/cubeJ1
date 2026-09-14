#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BLE monitor for the Cube J1.

Owns the Bluetooth UART (the stock ndblued/blueye must be stopped first),
brings up hci0 with the vendor patchram tool, and runs an LE scan forever.
Every PUBLISH_SEC it sends what it currently hears to the local MQTT hub on
topic cubej/<id>/ble/state: a per-device list and a count per device kind.

It classifies each advertiser only by public advertising data (company id,
service UUIDs, name) - never anything that identifies a person; addresses
rotate and are treated as opaque, kept only long enough to age a device out.

Talks to the same 127.0.0.1:11883 MQTT hub as mqtt_bridge.py, reusing that
file's tiny client. No cloud, no Bluetooth pairing, receive-only.
"""
import ctypes
import ctypes.util
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, "/data/local")
try:
    from mqtt_bridge import MQTTClient
except Exception:
    MQTTClient = None

CONFIG_PATH = "/data/local/config.json"
LOG_PATH    = "/data/local/ble_monitor.log"
HCI_DEV     = "/sys/class/bluetooth/hci0"
TTY         = "/dev/ttyS0"
FIRMWARE    = "/system/etc/firmware/BCM4345C0.hcd"
PATCHRAM    = "/usr/bin/brcm_patchram_plus2"
RFKILL      = "/sys/class/rfkill/rfkill0/state"

MQTT_HOST, MQTT_PORT = "127.0.0.1", 11883
PUBLISH_SEC = 30       # how often to publish the current picture
GONE_SEC    = 90       # a device unseen this long drops off the list
SCAN_SECS   = None     # run forever

AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI = 31, 3, 1
SOL_HCI, HCI_FILTER = 0, 2


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        f = open(LOG_PATH, "a")
        try:
            f.write(line)
        finally:
            f.close()
    except Exception:
        sys.stderr.write(line)


def sh(cmd):
    return subprocess.call(cmd, shell=True)


# --------------------------------------------------------------------------
# Bring hci0 up / take it down
# --------------------------------------------------------------------------

def stop_stock():
    sh("stop blueye")
    sh("stop ndblued")
    sh("for p in $(ps | grep brcm_patchram_plus2 | grep -v grep | "
       "awk '{print $2}'); do kill -9 $p 2>/dev/null; done")
    time.sleep(2)


def power_cycle():
    try:
        open(RFKILL, "w").write("0")
        time.sleep(1)
        open(RFKILL, "w").write("1")
        time.sleep(1)
    except Exception as e:
        log("rfkill power cycle failed: %s" % e)


def bring_up_hci():
    if os.path.exists(HCI_DEV):
        return True
    subprocess.Popen(
        [PATCHRAM, "--enable_hci", "--no2bytes", "--tosleep", "200000",
         "--baudrate", "115200", "--patchram", FIRMWARE, TTY],
        stdout=open("/data/local/tmp/patchram.log", "a"),
        stderr=subprocess.STDOUT)
    for _ in range(40):
        if os.path.exists(HCI_DEV):
            time.sleep(2)
            sh("hciconfig hci0 up")
            return True
        time.sleep(1)
    return False


def restore_stock():
    sh("for p in $(ps | grep brcm_patchram_plus2 | grep -v grep | "
       "awk '{print $2}'); do kill $p 2>/dev/null; done")
    time.sleep(2)
    sh("for p in $(ps | grep brcm_patchram_plus2 | grep -v grep | "
       "awk '{print $2}'); do kill -9 $p 2>/dev/null; done")
    for _ in range(10):
        if not os.path.exists(HCI_DEV):
            break
        time.sleep(1)
    power_cycle()
    sh("start ndblued")
    sh("start blueye")


# --------------------------------------------------------------------------
# Raw HCI socket LE scan
# --------------------------------------------------------------------------

def load_libc():
    for name in (None, ctypes.util.find_library("c"), "libc.so"):
        try:
            lib = ctypes.CDLL(name, use_errno=True)
            lib.socket
            return lib
        except Exception:
            continue
    raise RuntimeError("no libc")


class HciScanner(object):
    def __init__(self):
        self.libc = load_libc()
        self.fd = self.libc.socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)
        if self.fd < 0:
            raise OSError("HCI socket: errno %d" % ctypes.get_errno())
        addr = ctypes.create_string_buffer(struct.pack("<HHH", AF_BLUETOOTH, 0, 0))
        if self.libc.bind(self.fd, addr, 6) != 0:
            raise OSError("HCI bind: errno %d" % ctypes.get_errno())
        flt = struct.pack("<IIIH", 1 << 4,
                          (1 << 0x0E) | (1 << 0x0F), 1 << (0x3E - 32), 0)
        fbuf = ctypes.create_string_buffer(flt)
        if self.libc.setsockopt(self.fd, SOL_HCI, HCI_FILTER, fbuf, len(flt)) != 0:
            raise OSError("HCI filter: errno %d" % ctypes.get_errno())

    def cmd(self, ogf, ocf, params):
        op = (ogf << 10) | ocf
        os.write(self.fd, b"\x01" + struct.pack("<HB", op, len(params)) + params)
        end = time.time() + 2
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if not r:
                continue
            pkt = os.read(self.fd, 260)
            if len(pkt) >= 7 and _b(pkt[1]) == 0x0E and struct.unpack("<H", pkt[4:6])[0] == op:
                return _b(pkt[6])
        return None

    def start(self):
        self.cmd(0x08, 0x0001, b"\x1f" + b"\x00" * 7)          # LE event mask
        self.cmd(0x08, 0x000B, b"\x01\xa0\x00\xa0\x00\x00\x00")  # active, 100ms
        self.cmd(0x08, 0x000C, b"\x01\x00")                    # enable, no dup filter

    def stop(self):
        try:
            self.cmd(0x08, 0x000C, b"\x00\x00")
        except Exception:
            pass

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass

    def reports(self, timeout):
        """Yield (addr_type, addr_bytes, adv_data, rssi) until timeout."""
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return
            r, _, _ = select.select([self.fd], [], [], min(left, 1.0))
            if not r:
                continue
            pkt = os.read(self.fd, 260)
            for rep in parse_adv_reports(pkt):
                yield rep


def _b(c):
    return c if isinstance(c, int) else ord(c)


def parse_adv_reports(pkt):
    """Yield (addr_type, addr_bytes_msb_first, adv_data, rssi) from one HCI
    LE Advertising Report event packet:
      [0]=0x04 [1]=0x3E [2]=plen [3]=0x02(subevent) [4]=num_reports
      then per report: event_type, addr_type, addr[6], data_len, data, rssi
    """
    if len(pkt) < 5 or _b(pkt[1]) != 0x3E or _b(pkt[3]) != 0x02:
        return
    num = _b(pkt[4])
    p = 5
    for _ in range(num):
        if p + 9 > len(pkt):
            return
        atype = _b(pkt[p + 1])
        addr = pkt[p + 2:p + 8][::-1]
        dlen = _b(pkt[p + 8])
        data = pkt[p + 9:p + 9 + dlen]
        rssi = _b(pkt[p + 9 + dlen]) if p + 9 + dlen < len(pkt) else 0
        if rssi > 127:
            rssi -= 256
        p += 10 + dlen
        yield atype, addr, data, rssi


def ad_fields(data):
    i = 0
    while i < len(data):
        n = _b(data[i])
        if n == 0 or i + 1 + n > len(data):
            break
        yield _b(data[i + 1]), data[i + 2:i + 1 + n]
        i += 1 + n


# --------------------------------------------------------------------------
# Classify by public advertising data only
# --------------------------------------------------------------------------

def classify(data):
    name = None
    company = None
    services = []
    for ftype, val in ad_fields(data):
        if ftype in (0x08, 0x09):
            name = val.decode("utf-8", "replace")
        elif ftype == 0xFF and len(val) >= 2:
            company = _b(val[0]) | (_b(val[1]) << 8)
        elif ftype in (0x02, 0x03) and len(val) >= 2:
            for j in range(0, len(val) - 1, 2):
                services.append(_b(val[j]) | (_b(val[j + 1]) << 8))
        elif ftype == 0x16 and len(val) >= 2:
            services.append(_b(val[0]) | (_b(val[1]) << 8))

    # Apple: iPhone/Watch/Mac use company 0x004C; the first data byte is a type.
    if company == 0x004C:
        return "Apple", name
    if company == 0x0006:
        return "Windows", name
    if 0xFEB8 in services:
        return "Meta/Quest", name
    if company == 0x00E0 or 0xFE9F in services or 0xFCF1 in services or 0xFEF3 in services:
        return "Android/Google", name
    if company == 0x0969:
        return "Samsung", name
    if 0xFEED in services or 0xFD3D in services:
        return "Tile/Tracker", name
    if name and name.lower().startswith("remo"):
        return "Nature Remo", name
    if name:
        return "Named device", name
    return "Other", name


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def load_device_id():
    try:
        cfg = json.load(open(CONFIG_PATH))
        return str(cfg.get("device_id") or cfg.get("client_id") or "cubej")
    except Exception:
        return "cubej"


def snapshot(devices, now):
    """Fold the live device table into a JSON-ready message."""
    counts = {}
    out = []
    for d in devices.values():
        counts[d["kind"]] = counts.get(d["kind"], 0) + 1
        out.append({
            "kind": d["kind"],
            "name": d["name"],
            "rssi": d["rssi"],
            "seen": int(now - d["last"]),
            "count": d["hits"],
        })
    out.sort(key=lambda r: r["rssi"], reverse=True)
    return {"ts": int(now), "total": len(out), "counts": counts, "devices": out}


def main():
    if MQTTClient is None:
        log("cannot import MQTTClient from mqtt_bridge.py; aborting")
        return
    device_id = load_device_id()
    topic = "cubej/%s/ble/state" % device_id

    # `stop ble_monitor` sends SIGTERM; turn it into the same clean shutdown as
    # Ctrl-C so the finally block always hands the Bluetooth UART back.
    def _term(sig, frm):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)

    stop_stock()
    power_cycle()
    if not bring_up_hci():
        log("hci0 did not come up; restoring stock and aborting")
        restore_stock()
        return
    log("hci0 up, starting LE scan")

    mqtt = MQTTClient(MQTT_HOST, MQTT_PORT, "%s-ble" % device_id)
    try:
        mqtt.connect()
    except Exception as e:
        log("MQTT connect failed (%s); publishing will retry" % e)

    scanner = HciScanner()
    scanner.start()
    devices = {}     # addr hex -> {kind, name, rssi, last, hits}
    last_pub = 0.0
    try:
        while True:
            for atype, addr, data, rssi in scanner.reports(2.0):
                key = addr.hex() if hasattr(addr, "hex") else "".join("%02x" % _b(c) for c in addr)
                kind, name = classify(data)
                d = devices.get(key)
                if d is None:
                    d = devices[key] = {"kind": kind, "name": name, "rssi": rssi, "last": 0, "hits": 0}
                d["rssi"] = rssi
                d["last"] = time.time()
                d["hits"] += 1
                if kind != "Other":
                    d["kind"] = kind
                if name:
                    d["name"] = name
            now = time.time()
            for key in list(devices):
                if now - devices[key]["last"] > GONE_SEC:
                    del devices[key]
            if now - last_pub >= PUBLISH_SEC:
                msg = snapshot(devices, now)
                try:
                    mqtt.publish(topic, msg)
                except Exception as e:
                    log("publish failed: %s" % e)
                last_pub = now
    except KeyboardInterrupt:
        pass
    finally:
        log("scan ending; restoring stock Bluetooth")
        scanner.stop()
        scanner.close()
        restore_stock()


if __name__ == "__main__":
    main()
