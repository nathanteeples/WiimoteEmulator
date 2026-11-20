FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential pkg-config curl ca-certificates xz-utils patch libdbus-1-dev libglib2.0-dev libsdl1.2-dev libudev-dev libreadline-dev libusb-dev flex bison && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/wiimote
COPY . .
RUN curl -fsSL https://www.kernel.org/pub/linux/bluetooth/bluez-4.101.tar.xz -o bluez.tar.xz \
 && echo '41f9578bef39b8c94a2d6ddeaa556afd22d136936d0f03100e422fe970a45a7d  bluez.tar.xz' | sha256sum -c - \
 && tar -xf bluez.tar.xz && cd bluez-4.101 \
 && patch -p0 < ../bluez-disable-sdp.patch \
 && sed -i '/btd_\*;/a\        device_get_address;' src/bluetooth.ver \
 && CFLAGS='-O2 -fcommon' ./configure --prefix=/opt/wiimote/bluez-4.101/dist --sysconfdir=/etc --localstatedir=/var --with-systemdunitdir=/tmp/system --disable-service --disable-audio --disable-input --disable-serial --disable-udev --disable-cups --disable-test --disable-tools \
 && make -j2 && make install \
 && cd ../bluez-plugin && make && mkdir -p ../bluez-4.101/dist/lib/bluetooth/plugins \
 && cp wmemu.so ../bluez-4.101/dist/lib/bluetooth/plugins/ \
 && cd .. && CUSTOM_BUILD=1 make wmemulator wmwake
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv dbus libglib2.0-0 libsdl1.2debian libudev1 ca-certificates tini && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/wiimote
COPY --from=build /opt/wiimote/bluez-4.101/dist ./bluez-4.101/dist
COPY --from=build /opt/wiimote/wmemulator ./wmemulator
COPY --from=build /opt/wiimote/wmwake ./wmwake
COPY --from=build /etc/dbus-1/system.d/bluetooth.conf /etc/dbus-1/system.d/bluetooth.conf
COPY --from=build /opt/wiimote/bluez-4.101/src/main.conf /etc/bluetooth/main.conf
COPY eeprom.bin .
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
COPY docker-entrypoint.sh /usr/local/bin/
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
# Run link/import checks on the GitHub builder for each target architecture.
RUN ldd ./wmemulator > /tmp/links && ! grep -q 'not found' /tmp/links \
 && ldd ./wmwake > /tmp/links && ! grep -q 'not found' /tmp/links \
 && ldd ./bluez-4.101/dist/sbin/bluetoothd > /tmp/links && ! grep -q 'not found' /tmp/links \
 && /opt/venv/bin/python -c 'import flask; import paho.mqtt.client' \
 && ./bluez-4.101/dist/sbin/bluetoothd --version
