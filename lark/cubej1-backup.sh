#!/bin/sh
# Cube の記録を、毎晩 lark に写す。
#
# 30分ごとの記録（最大400日）・1分ごとの記録・BLE の台数は、Cube の内蔵ストレージに
# しか無い。lark は24時間動いていて場所もあるので、毎晩写して日付ごとに60日分残す。
#
# adb で取るのは、戻すときにそのまま使えるファイルそのものが欲しいから。
# Cube の adb は同時に1台しかつながらないので、ほかの機器（オーナーの PC）が
# つないでいたら、すぐ諦めずに待ってやり直す。
#
# 写さないもの: config.json（Bルートの ID とパスワード。紙で持っているし、
# ほかの場所に広げない）、ログ、プログラム（どれもリポジトリにある）。
set -eu

CUBE="${CUBEJ1_CUBE:-192.168.1.40:5555}"
DEST="${CUBEJ1_BACKUP_DIR:-/srv/cubej1-backup}"
KEEP_DAYS="${CUBEJ1_KEEP_DAYS:-60}"

# 記録と、Cube にしか無い設定
FILES="meter_daily.json meter_ring.json ble_hist.json
kindash/kindash_config.json kindash/station.json"
# 作ってあるときだけあるもの
OPTIONAL="tariff.json"
# production_tool が上書きする前に退避した純正の設定。ほかのどこにも無く、
# README の「元に戻す」で使う
STOCK="stock_wisund.rc stock_ndeclite_agent.rc stock_gndctrl.rc stock_ndblued.rc
stock_blueye.rc stock_tlsdated.rc stock_iptables_firewall.conf stock_iptables_firewall.sh"

day="$(TZ=Asia/Tokyo date +%Y-%m-%d)"
work="$DEST/.partial-$day"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

cleanup() {
    adb disconnect "$CUBE" >/dev/null 2>&1 || true
    adb kill-server >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

connected=no
for try in 1 2 3 4 5 6; do
    adb connect "$CUBE" >/dev/null 2>&1 || true
    if [ "$(adb -s "$CUBE" get-state 2>/dev/null)" = "device" ]; then
        connected=yes
        break
    fi
    log "Cube の adb につながらない（${try}回目）。ほかの機器がつないでいるかもしれないので待つ"
    adb disconnect "$CUBE" >/dev/null 2>&1 || true
    sleep 300
done
[ "$connected" = yes ] || { log "諦めた: Cube の adb につながらなかった"; exit 1; }

rm -rf "$work"
mkdir -p "$work/kindash" "$work/stock"

for f in $FILES; do
    adb -s "$CUBE" pull "/data/local/$f" "$work/$f" >/dev/null
done
for f in $OPTIONAL; do
    adb -s "$CUBE" pull "/data/local/$f" "$work/$f" >/dev/null 2>&1 || true
done
for f in $STOCK; do
    adb -s "$CUBE" pull "/data/local/$f" "$work/stock/$f" >/dev/null 2>&1 || true
done

# meter_hub は一時ファイルに書いてから名前を付け替えるので、取れるのは古いか新しいかの
# どちらか。それでも確かめる。途中で切れた写しで latest を置き換えるくらいなら、
# 昨日の分を残すほうがいい。
for f in $(cd "$work" && find . -name '*.json'); do
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$work/$f" \
        || { log "$f の写しが壊れている。昨日の分を残す"; exit 1; }
done

rm -rf "$DEST/$day"
mv "$work" "$DEST/$day"
ln -sfn "$day" "$DEST/latest"
find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +"$KEEP_DAYS" -exec rm -rf {} +

days_kept="$(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' | wc -l)"
log "${day} を保存: $(du -sh "$DEST/$day" | cut -f1)、${days_kept}日分を保持"
