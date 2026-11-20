import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ['DATA_DIR'] = tempfile.mkdtemp()
os.environ['WEB_TOKEN'] = 'test-secret'
from bridge.app import create_app, validate, DEFAULT, Bridge


class Tests(unittest.TestCase):
    def test_validation(self):
        for cfg in ({'console': '; rm -rf /'}, {'mqtt_port': 0}, {'mqtt_tls': 'false'}):
            with self.assertRaises(ValueError):
                validate(cfg)
        self.assertEqual(validate({'console': 'ab:cd:ef:01:23:45'})['console'], 'AB:CD:EF:01:23:45')

    def test_auth_and_csrf(self):
        bridge = Mock()
        app = create_app(bridge).test_client()
        self.assertEqual(app.get('/api/config').status_code, 401)
        auth = {'Authorization': 'Basic dXNlcjp0ZXN0LXNlY3JldA=='}
        self.assertEqual(app.post('/api/command', data='command=off', headers=auth).status_code, 415)
        self.assertEqual(app.post('/api/command', json={'command': 'off'}, headers=auth).status_code, 202)
        bridge.submit.assert_called_once_with('off')

    def test_password_not_exposed(self):
        import threading
        bridge = Mock(cfg={**DEFAULT, 'mqtt_password': 'private'}, lock=threading.RLock())
        response = create_app(bridge).test_client().get('/api/config', headers={'Authorization': 'Basic dXNlcjp0ZXN0LXNlY3JldA=='})
        self.assertNotIn(b'private', response.data)

    def test_retained_mqtt_command_ignored(self):
        with patch('bridge.app.threading.Thread'), patch('bridge.app.mqtt.Client') as cls:
            bridge = Bridge()
            bridge.cfg['mqtt_host'] = 'broker'
            bridge.configure_mqtt()
            bridge.submit = Mock()
            msg = Mock(topic='wiimote/bridge/command', payload=b'off', retain=True)
            cls.return_value.on_message(None, None, msg)
            bridge.submit.assert_not_called()
            msg.retain = False
            cls.return_value.on_message(None, None, msg)
            bridge.submit.assert_called_once_with('off')

    def test_reject_unconfigured_and_concurrent_commands(self):
        with patch('bridge.app.threading.Thread'):
            bridge = Bridge()
            with self.assertRaises(ValueError):
                bridge.submit('off')
            bridge.submit('pair')
            with self.assertRaises(ValueError):
                bridge.submit('pair')

if __name__ == '__main__':
    unittest.main()
