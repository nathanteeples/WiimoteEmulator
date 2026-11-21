"""One radio, two private BlueZ implementations; keep the selected family warm."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class BluetoothStack:
    def __init__(self, log):
        self.log, self.family, self.proc = log, None, None

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.family = None

    def ensure(self, family):
        if os.getenv('MANAGE_BLUETOOTH') != '1':
            return
        if family == self.family and self.proc and self.proc.poll() is None:
            return
        self.close()
        storage = Path('/data/bluez5' if family == 'switch' else '/data/bluetooth')
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        link = Path('/var/lib/bluetooth')
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise RuntimeError('/var/lib/bluetooth must be the managed data symlink')
        link.symlink_to(storage)
        daemon = '/usr/libexec/bluetooth/bluetoothd' if family == 'switch' else '/opt/wiimote/bluez-4.101/dist/sbin/bluetoothd'
        args = [daemon, '-n']
        if family == 'switch':
            args += ['-P', 'input']
        self.log('Starting Bluetooth stack: ' + family)
        self.proc = subprocess.Popen(args)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('Bluetooth daemon exited; check adapter and logs')
            result = subprocess.run(['dbus-send', '--system', '--print-reply', '--reply-timeout=500',
                '--dest=org.bluez', '/', 'org.freedesktop.DBus.Introspectable.Introspect'],
                capture_output=True, timeout=2)
            marker = b'ObjectManager' if family == 'switch' else b'org.bluez.Manager'
            if result.returncode == 0 and marker in result.stdout:
                if family == 'switch':
                    import dbus
                    selected = os.getenv('HCI_DEVICE', 'hci0')
                    bus = dbus.SystemBus()
                    objects = dbus.Interface(bus.get_object('org.bluez', '/'),
                        'org.freedesktop.DBus.ObjectManager').GetManagedObjects()
                    adapter = next((str(path) for path, interfaces in objects.items()
                        if 'org.bluez.Adapter1' in interfaces and
                        (str(path).rsplit('/', 1)[-1] == selected or
                         str(interfaces['org.bluez.Adapter1']['Address']).upper() == selected.upper())), None)
                    if adapter is None:
                        self.close()
                        raise RuntimeError('Configured Bluetooth adapter is unavailable: ' + selected)
                    props = dbus.Interface(bus.get_object('org.bluez', adapter), 'org.freedesktop.DBus.Properties')
                    props.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(True))
                    subprocess.run(['hciconfig', adapter.rsplit('/', 1)[-1], 'sspmode', '1'],
                                   capture_output=True, timeout=3, check=True)
                self.family = family
                return
            time.sleep(.1)
        self.close()
        raise RuntimeError('Bluetooth stack did not become ready')


class SwitchSession:
    """A warm joycontrol process avoids Bluetooth reconnects for repeated HOME presses."""
    def __init__(self, log):
        self.log, self.proc, self.address = log, None, None
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.last_used = 0
        self.reader = None

    def close(self):
        with self.lock:
            if self.proc:
                if self.proc.poll() is None:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait()
                if self.reader:
                    self.reader.join(timeout=2)
            self.proc, self.address = None, None

    def _read(self, proc, events):
        for line in proc.stdout:
            self.log(line.rstrip())
            if line.startswith('BRIDGE_EVENT '):
                try:
                    events.put(json.loads(line[len('BRIDGE_EVENT '):]))
                except ValueError:
                    pass
        events.put({'event': 'closed'})

    def command(self, action, address):
        warm = (action != 'pair' and address == self.address and self.proc and self.proc.poll() is None)
        if not warm:
            self.close()
            self.events = queue.Queue()
            args = ['/opt/venv/bin/python', '-u', '-m', 'bridge.switch_runner',
                    'pair' if action == 'pair' else 'home', address or '-']
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
            self.reader = threading.Thread(target=self._read, args=(self.proc, self.events), daemon=True)
            self.reader.start()
        else:
            self.log('Reusing active Switch connection')
            try:
                self.proc.stdin.write('home\n')
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.close()
                raise RuntimeError('Switch disconnected; retry HOME to reconnect') from exc
        deadline = time.monotonic() + (int(os.getenv('PAIR_TIMEOUT', '300')) if action == 'pair' else 35)
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=min(1, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if event['event'] in ('paired', 'home'):
                self.address = event['address']
                self.last_used = time.monotonic()
                return self.address
            if event['event'] in ('error', 'closed'):
                self.close()
                raise RuntimeError(event.get('message', 'Switch connection closed; check Bluetooth logs'))
        self.close()
        raise RuntimeError('Switch timed out. Pair in Controllers → Change Grip/Order. HOME wake requires an authenticated connection; no wake confirmed.')
