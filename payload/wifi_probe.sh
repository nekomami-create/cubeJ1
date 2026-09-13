#!/system/bin/sh
#
# One-shot Wi-Fi survey, run from production_tool when this file is on the USB.
#
# It changes nothing: it asks wpa_supplicant to scan (once plainly, then once
# per named SSID so that a stealth AP also answers) and writes what it saw to
# /data/local/wifi_probe.txt and to the USB stick. Scans cover both 2.4GHz
# and 5GHz; the freq column of scan_results tells them apart.
#
# The SSIDs to look for are not in the repository. They are read from
# wifi_probe_ssid.txt next to this script, one per line as "<hex> <name>":
# wpa_cli's "scan ssid" wants hex, and the Cube has no reliable text-to-hex
# tool, so the PC side writes both.

PT="/tmp/production_tool"
W="wpa_cli -p /data/misc/wifi/sockets -i wlan0"
OUT=/data/local/wifi_probe.txt
LIST=$PT/wifi_probe_ssid.txt

{
    echo "== $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
    echo "== versions"
    getprop ro.build.version.release
    $W -v 2>&1 | head -1
    echo "== status (current connection)"
    $W status
    $W signal_poll

    echo "== plain scan"
    $W scan
    sleep 10
    $W scan_results

    if [ -f $LIST ]; then
        tr -d '\r' < $LIST | while read HEX NAME; do
            [ -n "$HEX" ] || continue
            echo "== directed scan for '$NAME' (ssid $HEX)"
            $W scan ssid $HEX
            sleep 10
            $W scan_results
            if command -v iw >/dev/null 2>&1; then
                echo "-- iw directed scan for '$NAME'"
                iw dev wlan0 scan ssid "$NAME" 2>&1 | grep -E '^BSS|SSID:|signal:|freq:'
            fi
        done
    else
        echo "== no wifi_probe_ssid.txt, directed scans skipped"
    fi
    echo "== end"
} > $OUT 2>&1

# The stick is mounted vfat, usually at /mnt/usb. Look it up rather than trust
# that, and copy to every candidate so the result survives a missing network.
for d in $(grep -i vfat /proc/mounts 2>/dev/null | awk '{print $2}') /mnt/usb; do
    [ -d "$d" ] && cp $OUT "$d/wifi_probe.txt" 2>/dev/null
done
sync
