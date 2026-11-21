"""Single-process Wii control bridge. Hardware actions are serialized."""
import atexit
import hmac
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt

DATA = Path(os.getenv('DATA_DIR', '/data'))
DEFAULT = dict(console='', mqtt_host='', mqtt_port=1883, mqtt_user='', mqtt_password='', mqtt_tls=False, consoles=[], default_console='')


def validate(raw):
    cfg = {k: raw.get(k, v) for k, v in DEFAULT.items()}
    for k in ('console', 'mqtt_host', 'mqtt_user', 'mqtt_password'):
        if not isinstance(cfg[k], str) or len(cfg[k]) > 512:
            raise ValueError('Invalid ' + k)
    cfg['console'] = cfg['console'].strip().upper()
    if cfg['console'] and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', cfg['console']):
        raise ValueError('Console must be a Bluetooth MAC address')
    if type(cfg['mqtt_port']) is not int or not 1 <= cfg['mqtt_port'] <= 65535:
        raise ValueError('MQTT port must be 1–65535')
    if type(cfg['mqtt_tls']) is not bool:
        raise ValueError('Invalid TLS setting')
    profiles = cfg['consoles']
    if not isinstance(profiles, list) or len(profiles) > 32:
        raise ValueError('Save at most 32 consoles')
    if not profiles and cfg['console']:
        profiles = [dict(id='wiiu', label='Wii U', kind='wiiu', address=cfg['console'])]
    cleaned, ids, addresses = [], set(), set()
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError('Invalid console profile')
        ident, label = profile.get('id', ''), profile.get('label', '')
        kind, address = profile.get('kind', 'wiiu'), profile.get('address', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z0-9_-]{1,48}', ident) or ident in ids:
            raise ValueError('Console IDs must be unique letters, numbers, underscores or hyphens')
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('Console label must contain 1–80 characters')
        if kind not in ('wii', 'wiiu', 'switch') or not isinstance(address, str):
            raise ValueError('Invalid console type or address')
        address = address.strip().upper()
        if address and (not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address) or address in addresses):
            raise ValueError('Console Bluetooth addresses must be valid and unique')
        ids.add(ident)
        if address:
            addresses.add(address)
        cleaned.append(dict(id=ident, label=label.strip(), kind=kind, address=address))
    cfg['consoles'] = cleaned
    if cfg['default_console'] not in ids:
        cfg['default_console'] = cleaned[0]['id'] if cleaned else ''
    cfg['console'] = next((c['address'] for c in cleaned if c['id'] == cfg['default_console']), '')
    return cfg


class Bridge:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / 'config.json'
        self.cfg = validate(json.loads(path.read_text())) if path.exists() else validate({})
        if path.exists():
            self.persist()
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=4)
        self.proc = None
        self.connected = threading.Event()
        self.power_sent = threading.Event()
        self.paired = None
        self.client = None
        self.last_wake_request = 0
        self.wake_requests = {}
        self.active_console = None
        from bridge.bluetooth import BluetoothStack, SwitchSession
        self.stack = BluetoothStack(self.log)
        self.switch = SwitchSession(self.log)
        try:
            state = json.loads((DATA / 'wake-state.json').read_text())
            self.wake_requests = state.get('requests', {})
            self.last_wake_request = float(state.get('last_request', 0))
            if self.last_wake_request and self.cfg['console']:
                self.wake_requests[self.cfg['console']] = self.last_wake_request
        except (OSError, ValueError, TypeError):
            pass
        self.wake_sent = threading.Event()
        self.wake_confirmed = threading.Event()
        self.already_awake = threading.Event()
        self.recent_wake = threading.Event()
        self.status = dict(action='idle', result='No command sent', mqtt=False, logs=[])
        self.log('Console bridge started; Bluetooth adapter: ' + os.getenv('HCI_DEVICE', 'hci0'))
        threading.Thread(target=self.worker, daemon=True).start()
        self.configure_mqtt()

    def persist(self):
        temp = DATA / 'config.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.cfg, f)
        os.replace(temp, DATA / 'config.json')

    def shutdown(self):
        self.stop()
        self.switch.close()
        self.stack.close()

    def log(self, message):
        line = time.strftime('%H:%M:%S') + ' ' + message
        with self.lock:
            self.status['logs'].append(line)
            self.status['logs'] = self.status['logs'][-30:]
        print(line, flush=True)

    def publish(self, suffix, payload, retain=False):
        if self.client:
            self.client.publish('wiimote/bridge/' + suffix, payload, qos=1, retain=retain)

    def discovery(self):
        topics = set()
        for profile in self.cfg['consoles']:
            actions = ('home',) if profile['kind'] == 'switch' else ('on', 'off')
            for action in actions:
                ident = 'wiimote_bridge_' + profile['id'] + '_' + action
                topic = 'homeassistant/button/' + ident + '/config'
                topics.add(topic)
                payload = dict(name='HOME / Wake' if action == 'home' else 'Power ' + action,
                    unique_id=ident, command_topic='wiimote/bridge/consoles/' + profile['id'] + '/command',
                    payload_press=action, availability_topic='wiimote/bridge/availability',
                    device=dict(identifiers=['wiimote_bridge_' + profile['id']], name=profile['label']))
                self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        # Preserve the original Home Assistant buttons for existing automations.
        for action in ('on', 'off'):
            topic = 'homeassistant/button/wiimote_bridge_' + action + '/config'
            default = next((p for p in self.cfg['consoles'] if p['id'] == self.cfg['default_console']), None)
            if default and (default['kind'] != 'switch' or action == 'on'):
                topics.add(topic)
                self.client.publish(topic, json.dumps(dict(name='Power ' + action,
                    unique_id='wiimote_bridge_' + action, command_topic='wiimote/bridge/command',
                    payload_press=action, availability_topic='wiimote/bridge/availability',
                    device=dict(identifiers=['wiimote_bridge'], name='Console bridge default'))), qos=1, retain=True)
            else:
                self.client.publish(topic, '', qos=1, retain=True)
        path = DATA / 'discovery-topics.json'
        try:
            previous = set(json.loads(path.read_text()))
        except (OSError, ValueError):
            previous = set()
        for topic in previous - topics:
            self.client.publish(topic, '', qos=1, retain=True)
        path.write_text(json.dumps(sorted(topics)))

    def configure_mqtt(self):
        if self.client:
            self.client.publish('wiimote/bridge/availability', 'offline', qos=1, retain=True).wait_for_publish(timeout=2)
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None
        self.status['mqtt'] = False
        cfg = self.cfg.copy()
        if not cfg['mqtt_host']:
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='wiimote-bridge')
        if cfg['mqtt_user']:
            client.username_pw_set(cfg['mqtt_user'], cfg['mqtt_password'])
        if cfg['mqtt_tls']:
            client.tls_set()
        client.will_set('wiimote/bridge/availability', 'offline', qos=1, retain=True)

        def connect(c, userdata, flags, reason, props):
            self.status['mqtt'] = not reason.is_failure
            if not reason.is_failure:
                c.subscribe('wiimote/bridge/command', qos=1)
                c.subscribe('wiimote/bridge/consoles/+/command', qos=1)
                c.subscribe('homeassistant/status')
                self.discovery()
                self.publish('availability', 'online', True)

        def message(c, userdata, msg):
            if msg.topic == 'homeassistant/status':
                if msg.payload == b'online':
                    self.discovery()
                return
            # Retained commands must never replay after reconnect/restart.
            if msg.retain:
                return
            try:
                ident = re.fullmatch(r'wiimote/bridge/consoles/([a-z0-9_-]+)/command', msg.topic)
                action = msg.payload.decode('ascii').strip().lower()
                if ident:
                    self.submit(action, ident.group(1))
                else:
                    self.submit(action)
            except (ValueError, UnicodeError) as exc:
                payload = json.dumps(dict(error=str(exc)))
                self.publish('result', payload)
                if ident:
                    self.publish('consoles/' + ident.group(1) + '/result', payload)
                self.log('MQTT command rejected: ' + str(exc))

        client.on_connect = connect
        client.on_disconnect = lambda *args: self.status.update(mqtt=False)
        client.on_message = message
        self.client = client
        client.connect_async(cfg['mqtt_host'], cfg['mqtt_port'], keepalive=30)
        client.loop_start()

    def save(self, raw):
        with self.lock:
            if self.status['action'] != 'idle' or not self.jobs.empty():
                raise ValueError('Wait for the active command before saving')
            if raw.get('mqtt_password') == '':
                raw['mqtt_password'] = self.cfg['mqtt_password']
            merged = {**self.cfg, **raw}
            # Legacy address editing updates only the default profile.
            if 'consoles' not in raw and 'console' in raw and raw['console'] != self.cfg['console']:
                merged['consoles'] = [dict(p, address=raw['console']) if p['id'] == self.cfg['default_console'] else p for p in self.cfg['consoles']]
            cfg = validate(merged)
            temp = DATA / 'config.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(cfg, f)
            os.replace(temp, DATA / 'config.json')
            self.cfg = cfg
        self.configure_mqtt()

    def profiles(self, raw):
        with self.lock:
            if self.status['action'] != 'idle' or not self.jobs.empty():
                raise ValueError('Wait for the active command before editing consoles')
            profiles = [dict(p) for p in self.cfg['consoles']]
            op = raw.get('operation', 'save')
            ident = raw.get('id')
            if op == 'delete':
                if not any(p['id'] == ident for p in profiles):
                    raise ValueError('Unknown console')
                profiles = [p for p in profiles if p['id'] != ident]
            elif op == 'default':
                if not any(p['id'] == ident for p in profiles):
                    raise ValueError('Unknown console')
            elif op == 'save':
                if not ident:
                    ident = uuid.uuid4().hex[:12]
                    profiles.append(dict(id=ident, label=raw.get('label'), kind=raw.get('kind', 'wiiu'), address=raw.get('address', '')))
                else:
                    profile = next((p for p in profiles if p['id'] == ident), None)
                    if profile is None:
                        raise ValueError('Unknown console')
                    profile.update({k: raw[k] for k in ('label', 'kind', 'address') if k in raw})
            else:
                raise ValueError('Unknown operation')
            cfg = {**self.cfg, 'consoles': profiles, 'console': ''}
            if op == 'default':
                cfg['default_console'] = ident
            self.cfg = validate(cfg)
            self.persist()
            self.switch.close()
        if self.client and self.status['mqtt']:
            self.discovery()
        return ident

    def submit(self, action, console_id=None):
        if action in ('wake', 'home'):
            action = 'on'
        if action not in ('on', 'off', 'pair'):
            raise ValueError('Command must be on, off, pair, home, or wake')
        with self.lock:
            profile = next((p for p in self.cfg['consoles'] if p['id'] == (console_id or self.cfg['default_console'])), None)
            if profile is None:
                if console_id or action != 'pair':
                    raise ValueError('Choose a saved console first')
                profile = dict(id='wiiu', label='Wii U', kind='wiiu', address='')
                self.cfg['consoles'].append(profile)
                self.cfg['default_console'] = profile['id']
            if profile['kind'] == 'switch' and action == 'off':
                raise ValueError('Switch supports HOME / wake; power off is not implemented')
            if action != 'pair' and not profile['address']:
                raise ValueError('Pair this console first or save its Bluetooth address')
            if self.status['action'] != 'idle' or not self.jobs.empty():
                raise ValueError('A command is already active')
            self.log(f"Command accepted: {action}: {profile['label']}")
            self.status.update(action=action, console_id=profile['id'], console_label=profile['label'],
                               result='Waiting for console pairing' if action == 'pair' else 'Sending command')
            cfg = {**self.cfg, 'console': profile['address'], 'profile': dict(profile)}
            self.jobs.put_nowait((action, cfg))

    def clear_wake_request(self):
        self.last_wake_request = 0
        self.wake_requests.pop(self.active_console, None)
        try:
            if self.wake_requests:
                self.persist_wake_requests()
            else:
                (DATA / 'wake-state.json').unlink(missing_ok=True)
        except OSError as exc:
            self.log('Unable to clear wake cooldown: ' + str(exc))

    def persist_wake_requests(self):
        temp = DATA / 'wake-state.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'requests': self.wake_requests}, f)
        os.replace(temp, DATA / 'wake-state.json')

    def read_output(self, proc):
        for line in proc.stdout:
            self.log(line.rstrip())
            match = re.search(r'connected to ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})', line)
            if match:
                self.paired = match.group(1).upper()
                self.connected.set()
            if 'WAKE_SIGNAL_SENT' in line:
                self.wake_sent.set()
                self.last_wake_request = time.time()
                try:
                    self.wake_requests[self.active_console] = self.last_wake_request
                    self.persist_wake_requests()
                except OSError as exc:
                    self.log('Unable to persist wake cooldown: ' + str(exc))
            if 'WAKE_CONFIRMED' in line:
                self.wake_confirmed.set()
                self.clear_wake_request()
            if 'WAKE_ALREADY_AWAKE' in line:
                self.already_awake.set()
            if 'WAKE_RECENT_REQUEST' in line:
                self.recent_wake.set()
            if 'POWER_OFF_SENT' in line:
                self.power_sent.set()
                self.clear_wake_request()

    def stop(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def wake_console(self, cfg):
        for event in (self.wake_sent, self.wake_confirmed, self.already_awake, self.recent_wake):
            event.clear()
        args = ['stdbuf', '-oL', './wmwake', cfg['console']]
        if time.time() - self.last_wake_request < 60:
            args.append('--probe-only')
        self.log('Checking console power and authenticated Bluetooth wake')
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        reader = threading.Thread(target=self.read_output, args=(self.proc,), daemon=True)
        reader.start()
        try:
            code = self.proc.wait(timeout=60)
            reader.join(timeout=2)
            if self.already_awake.is_set():
                return 'Console already awake; no power-button signal sent'
            if self.wake_confirmed.is_set():
                return 'Wake confirmed: console HID service is available'
            if self.recent_wake.is_set():
                return 'Recent wake request still pending; no second power-button signal sent'
            if self.wake_sent.is_set():
                return 'Wake signal sent; console HID service did not become ready within 35 seconds'
            raise RuntimeError('Wake failed before power-button signal (exit ' + str(code) + '); check Bluetooth log')
        finally:
            self.stop()
            reader.join(timeout=2)

    def execute(self, action, cfg):
        profile = cfg.get('profile', {'kind': 'wiiu', 'id': 'wiiu'})
        self.active_console = cfg['console']
        self.last_wake_request = self.wake_requests.get(self.active_console, 0)
        if profile['kind'] == 'switch':
            self.stack.ensure('switch')
            address = self.switch.command(action, cfg['console'])
            if action == 'pair':
                self.paired = address
                return 'Paired Switch ' + address
            return 'HOME sent over active Switch HID connection; physical wake unverified'
        self.switch.close()
        self.stack.ensure('wii')
        if action == 'on':
            return self.wake_console(cfg)
        self.connected.clear()
        self.power_sent.clear()
        self.paired = None
        sockpath = '/run/wiimote-input.sock'
        Path(sockpath).unlink(missing_ok=True)
        self.log(f'Starting emulator: action={action} adapter=' + os.getenv('HCI_DEVICE', 'hci0'))
        self.proc = subprocess.Popen(['stdbuf', '-oL', './wmemulator',
                                      'pair' if action == 'pair' else cfg['console'], 'unix', sockpath],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        reader = threading.Thread(target=self.read_output, args=(self.proc,), daemon=True)
        reader.start()
        deadline = time.monotonic() + (int(os.getenv('PAIR_TIMEOUT', '300')) if action == 'pair' else 30)
        try:
            while not self.connected.wait(.1):
                if self.proc.poll() is not None:
                    raise RuntimeError('Emulator exited before connecting; check container logs')
                if time.monotonic() > deadline:
                    raise RuntimeError('Bluetooth connection timed out')
            if action == 'off':
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                    sock.sendto(b'emulator_control 1 power_off', sockpath)
                if not self.power_sent.wait(5):
                    raise RuntimeError('Power-off command was not acknowledged by the adapter')
            return 'Paired with ' + str(self.paired) if action == 'pair' else 'Power ' + action + ' command sent; console state unverified'
        finally:
            self.stop()
            reader.join(timeout=2)

    def worker(self):
        try:
            profile = next((p for p in self.cfg['consoles'] if p['id'] == self.cfg['default_console']), {})
            self.stack.ensure('switch' if profile.get('kind') == 'switch' else 'wii')
        except Exception as exc:
            self.log('Bluetooth startup: ' + str(exc))
        while True:
            action, cfg = self.jobs.get()
            try:
                result = self.execute(action, cfg)
                if action == 'pair':
                    with self.lock:
                        profiles = [dict(p, address=self.paired) if p['id'] == cfg['profile']['id'] else p for p in self.cfg['consoles']]
                        self.cfg = validate({**self.cfg, 'consoles': profiles})
                        self.persist()
                    if self.client and self.status['mqtt']:
                        self.discovery()
            except Exception as exc:
                result = str(exc)
            with self.lock:
                self.status.update(action='idle', result=result)
            self.log(f'Command completed: {action}: {result}')
            payload = json.dumps(dict(command=action, result=result, console_id=cfg['profile']['id'], label=cfg['profile']['label']))
            self.publish('result', payload)
            self.publish('consoles/' + cfg['profile']['id'] + '/result', payload)
            self.jobs.task_done()


def create_app(bridge):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 8192

    @app.before_request
    def auth():
        token = os.environ.get('WEB_TOKEN', '')
        supplied = request.authorization.password if request.authorization else ''
        if not token or not hmac.compare_digest(supplied or '', token):
            return 'Authentication required', 401, {'WWW-Authenticate': 'Basic realm="Wii bridge"'}
        if request.method == 'POST' and not request.is_json:
            return jsonify(error='JSON required'), 415

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/config')
    def config():
        with bridge.lock:
            return jsonify({**bridge.cfg, 'mqtt_password': ''})

    @app.get('/api/status')
    def status():
        return jsonify(bridge.status)

    @app.post('/api/config')
    def save():
        try:
            bridge.save(request.get_json())
            return jsonify(ok=True)
        except (ValueError, TypeError, AttributeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/command')
    def command():
        try:
            data = request.get_json()
            if data.get('console_id'):
                bridge.submit(data.get('command'), data['console_id'])
            else:
                bridge.submit(data.get('command'))
            return jsonify(accepted=True), 202
        except (ValueError, AttributeError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/cancel')
    def cancel():
        bridge.stop()
        bridge.switch.close()
        return jsonify(ok=True)

    @app.post('/api/consoles')
    def consoles():
        try:
            ident = bridge.profiles(request.get_json())
            return jsonify(ok=True, id=ident)
        except (ValueError, TypeError, AttributeError) as exc:
            return jsonify(error=str(exc)), 400

    return app


if __name__ == '__main__':
    if not os.environ.get('WEB_TOKEN'):
        raise SystemExit('Set WEB_TOKEN to a strong password')
    bridge = Bridge()
    atexit.register(bridge.shutdown)
    signal.signal(signal.SIGTERM, lambda *args: exit(0))
    create_app(bridge).run(host='0.0.0.0', port=8080, threaded=True)
