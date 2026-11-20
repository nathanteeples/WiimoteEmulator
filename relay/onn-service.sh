#!/system/bin/sh
# Installed as /data/adb/service.d/wiimote-relay.sh. No SELinux changes.
BASE=/data/adb/wiimote-relay
(
  exec 9>"$BASE/supervisor.lock"
  /data/adb/magisk/busybox flock -n 9 || exit 0
  while [ "$(getprop sys.boot_completed)" != 1 ]; do sleep 5; done
  mkdir -p "$BASE/logs"
  while true; do
    [ -f "$BASE/disabled" ] && exit 0
    if [ -f "$BASE/logs/relay.log" ] && [ "$(wc -c < "$BASE/logs/relay.log")" -gt 1000000 ]; then
      mv -f "$BASE/logs/relay.log" "$BASE/logs/relay.previous.log"
    fi
    "$BASE/onn-usb-relay" "$BASE" 18765 00:1E:35:DA:71:16 >> "$BASE/logs/relay.log" 2>&1
    sleep 3
  done
) </dev/null >/dev/null 2>&1 &
