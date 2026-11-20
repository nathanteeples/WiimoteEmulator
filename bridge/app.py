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

from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt

DATA = Path(os.getenv('DATA_DIR', '/data'))
DEFAULT = dict(console='', mqtt_host='', mqtt_port=1883, mqtt_user='', mqtt_password='', mqtt_tls=False)


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
    return cfg


class Bridge:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / 'config.json'
        self.cfg = validate(json.loads(path.read_text())) if path.exists() else DEFAULT.copy()
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=4)
        self.proc = None
        self.connected = threading.Event()
        self.power_sent = threading.Event()
        self.paired = None
        self.client = None
        self.status = dict(action='idle', result='No command sent', mqtt=False, logs=[])
        self.log('Wii bridge started; Bluetooth adapter: ' + os.getenv('HCI_DEVICE', 'hci0'))
        threading.Thread(target=self.worker, daemon=True).start()
        self.configure_mqtt()

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
        for action in ('on', 'off'):
            payload = dict(name='Power ' + action, unique_id='wiimote_bridge_' + action,
                           command_topic='wiimote/bridge/command', payload_press=action,
                           availability_topic='wiimote/bridge/availability',
                           device=dict(identifiers=['wiimote_bridge'], name='Wii power bridge'))
            self.client.publish('homeassistant/button/wiimote_bridge_' + action + '/config',
                                json.dumps(payload), qos=1, retain=True)

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
                self.submit(msg.payload.decode('ascii').lower())
            except (ValueError, UnicodeError) as exc:
                self.publish('result', json.dumps(dict(error=str(exc))))

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
            cfg = validate(raw)
            temp = DATA / 'config.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(cfg, f)
            os.replace(temp, DATA / 'config.json')
            self.cfg = cfg
        self.configure_mqtt()

    def submit(self, action):
        if action not in ('on', 'off', 'pair'):
            raise ValueError('Command must be on, off, or pair')
        with self.lock:
            if action != 'pair' and not self.cfg['console']:
                raise ValueError('Configure the console Bluetooth address first')
            if self.status['action'] != 'idle' or not self.jobs.empty():
                raise ValueError('A command is already active')
            self.log(f'Command accepted: {action}')
            self.status['action'] = action
            self.status['result'] = 'Waiting for console pairing' if action == 'pair' else 'Sending power command'
            self.jobs.put_nowait((action, self.cfg.copy()))

    def read_output(self, proc):
        for line in proc.stdout:
            self.log(line.rstrip())
            match = re.search(r'connected to ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})', line)
            if match:
                self.paired = match.group(1).upper()
                self.connected.set()
            if 'POWER_OFF_SENT' in line:
                self.power_sent.set()

    def stop(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def execute(self, action, cfg):
        self.connected.clear()
        self.power_sent.clear()
        self.paired = None
        sockpath = '/run/wiimote-input.sock'
        Path(sockpath).unlink(missing_ok=True)
        self.log(f'Starting emulator: action={action} adapter=' + os.getenv('HCI_DEVICE', 'hci0'))
        self.proc = subprocess.Popen(['stdbuf', '-oL', './wmemulator',
                                      'pair' if action == 'pair' else cfg['console'], 'unix', sockpath],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
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

    def worker(self):
        while True:
            action, cfg = self.jobs.get()
            try:
                result = self.execute(action, cfg)
                if action == 'pair':
                    with self.lock:
                        self.cfg['console'] = self.paired
                        path = DATA / 'config.json'
                        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, 'w') as f:
                            json.dump(self.cfg, f)
            except Exception as exc:
                result = str(exc)
            with self.lock:
                self.status.update(action='idle', result=result)
            self.log(f'Command completed: {action}: {result}')
            self.publish('result', json.dumps(dict(command=action, result=result)))
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
            bridge.submit(request.get_json().get('command'))
            return jsonify(accepted=True), 202
        except (ValueError, AttributeError) as exc:
            return jsonify(error=str(exc)), 400

    return app


if __name__ == '__main__':
    if not os.environ.get('WEB_TOKEN'):
        raise SystemExit('Set WEB_TOKEN to a strong password')
    bridge = Bridge()
    atexit.register(bridge.stop)
    signal.signal(signal.SIGTERM, lambda *args: exit(0))
    create_app(bridge).run(host='0.0.0.0', port=8080, threaded=True)
