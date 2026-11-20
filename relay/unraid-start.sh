#!/bin/sh
BASE=/boot/config/plugins/wiimote-relay
mkdir -p /var/run/wiimote-relay
exec 9>/var/run/wiimote-relay/supervisor.lock
flock -n 9 || exit 0
while [ ! -f "$BASE/disabled" ]; do
  # Python may be installed by a plugin after /boot/config/go runs.
  if [ -x /usr/bin/python3 ] && [ -e /dev/vhci ]; then
    /usr/bin/python3 "$BASE/unraid_vhci_client.py" "$BASE"
  else
    [ -e /dev/vhci ] || modprobe hci_vhci 2>/dev/null || true
  fi
  sleep 3
done
