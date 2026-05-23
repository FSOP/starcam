#!/bin/bash
# Run once with sudo to enable LED control from the web app without root:
#   sudo bash ~/starcam/setup-leds.sh
set -e
echo 'SUBSYSTEM=="leds", RUN+="/bin/chmod a+rw /sys%p/brightness /sys%p/trigger"' \
  > /etc/udev/rules.d/99-starcam-leds.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=leds
echo "OK — LED control enabled (permanent)"
