import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from bridge.app import Bridge, validate

MAC1 = '40:D2:8A:43:4E:C8'
MAC2 = '00:11:22:33:44:55'

class Profiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch('bridge.app.DATA', Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with patch('bridge.app.threading.Thread'):
            self.b = Bridge()

    def test_legacy_config_migration_preserves_address_and_password(self):
        cfg = validate(dict(console=MAC1, mqtt_password='secret'))
        self.assertEqual(cfg['consoles'][0]['address'], MAC1)
        self.assertEqual(cfg['default_console'], 'wiiu')
        self.assertEqual(cfg['mqtt_password'], 'secret')

    def test_labels_and_addresses_survive_restart_and_mqtt_settings(self):
        one = self.b.profiles(dict(label='Media Wii U', address=MAC1))
        two = self.b.profiles(dict(label='Bedroom Switch', kind='switch', address=MAC2))
        self.b.save(dict(mqtt_host='', mqtt_password=''))
        with patch('bridge.app.threading.Thread'):
            restored = Bridge()
        self.assertEqual([p['label'] for p in restored.cfg['consoles']], ['Media Wii U', 'Bedroom Switch'])
        restored.profiles(dict(operation='default', id=two))
        self.assertEqual(restored.cfg['console'], MAC2)
        restored.submit('home', two)
        action, cfg = restored.jobs.get_nowait()
        self.assertEqual((action, cfg['console']), ('on', MAC2))
        self.assertEqual(cfg['profile']['id'], two)
        self.assertNotEqual(one, two)

    def test_duplicate_address_and_switch_off_rejected(self):
        ident = self.b.profiles(dict(label='Switch', kind='switch', address=MAC2))
        with self.assertRaises(ValueError):
            self.b.profiles(dict(label='Duplicate', address=MAC2))
        with self.assertRaises(ValueError):
            self.b.submit('off', ident)
        with self.assertRaises(ValueError):
            self.b.submit('home', 'unknown')

    def test_targeted_mqtt_and_retained_command_safety(self):
        ident = self.b.profiles(dict(label='Second console', address=MAC2))
        with patch('bridge.app.mqtt.Client') as client:
            self.b.cfg['mqtt_host'] = 'broker'
            self.b.configure_mqtt()
            self.b.submit = Mock()
            msg = Mock(topic=f'wiimote/bridge/consoles/{ident}/command', payload=b'on', retain=True)
            client.return_value.on_message(None, None, msg)
            self.b.submit.assert_not_called()
            msg.retain = False
            client.return_value.on_message(None, None, msg)
            self.b.submit.assert_called_once_with('on', ident)

    def test_cooldowns_are_separate(self):
        self.b.active_console = MAC1
        self.b.read_output(Mock(stdout=iter(['WAKE_SIGNAL_SENT\n'])))
        self.b.active_console = MAC2
        self.b.read_output(Mock(stdout=iter(['POWER_OFF_SENT\n'])))
        self.assertIn(MAC1, self.b.wake_requests)
        self.assertNotIn(MAC2, self.b.wake_requests)
        state = json.loads((Path(self.temp.name)/'wake-state.json').read_text())
        self.assertIn(MAC1, state['requests'])

    def test_deleted_discovery_topics_are_removed(self):
        ident = self.b.profiles(dict(label='Old console', address=MAC2))
        self.b.client = Mock()
        self.b.discovery()
        self.b.profiles(dict(operation='delete', id=ident))
        self.b.client.publish.assert_any_call(f'homeassistant/button/wiimote_bridge_{ident}_on/config', '', qos=1, retain=True)
        self.assertEqual(self.b.cfg['consoles'], [])
