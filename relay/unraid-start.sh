#!/bin/sh
BASE=/boot/config/plugins/wiimote-relay
# At boot Python may be installed by a plugin after /boot/config/go executes.
while [ ! -x /usr/bin/python3 ] || [ ! -e /dev/vhci ]; do
  [ -e /dev/vhci ] || modprobe hci_vhci 2>/dev/null || true
  sleep 5
done
exec /usr/bin/python3 "$BASE/unraid_vhci_client.py" "$BASE"
