import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from bridge.app import Bridge

class WakeTests(unittest.TestCase):
    def new_bridge(self):
        # Exercise wake state and results without starting MQTT or hardware threads.
        with patch('bridge.app.threading.Thread'):
            return Bridge()

    def test_confirmed_power_off_allows_immediate_wake(self):
        with tempfile.TemporaryDirectory() as d, patch('bridge.app.DATA', Path(d)):
            b=self.new_bridge()
            b.read_output(Mock(stdout=iter(['WAKE_SIGNAL_SENT\n'])))
            self.assertGreater(b.last_wake_request,0)
            self.assertTrue((Path(d)/'wake-state.json').exists())
            b.read_output(Mock(stdout=iter(['POWER_OFF_SENT\n'])))
            self.assertEqual(b.last_wake_request,0)
            self.assertFalse((Path(d)/'wake-state.json').exists())

    def test_unconfirmed_wake_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory() as d, patch('bridge.app.DATA', Path(d)):
            (Path(d)/'wake-state.json').write_text(json.dumps({'last_request':time.time()}))
            b=self.new_bridge();proc=Mock()
            proc.wait.side_effect=lambda **kw: (b.recent_wake.set() or 2)
            with patch('bridge.app.subprocess.Popen',return_value=proc) as spawn, patch('bridge.app.threading.Thread'):
                result=b.wake_console({'console':'40:D2:8A:43:4E:C8'})
            self.assertIn('--probe-only',spawn.call_args.args[0])
            self.assertIn('no second power-button',result)

    def test_exit_success_without_protocol_confirmation_is_not_success(self):
        with tempfile.TemporaryDirectory() as d, patch('bridge.app.DATA', Path(d)):
            b=self.new_bridge();proc=Mock();proc.wait.return_value=0
            with patch('bridge.app.subprocess.Popen',return_value=proc), patch('bridge.app.threading.Thread'):
                with self.assertRaises(RuntimeError):
                    b.wake_console({'console':'40:D2:8A:43:4E:C8'})

    def test_existing_awake_console_does_not_mark_new_wake(self):
        with tempfile.TemporaryDirectory() as d, patch('bridge.app.DATA', Path(d)):
            b=self.new_bridge();proc=Mock();proc.wait.side_effect=lambda **kw: (b.already_awake.set() or 0)
            with patch('bridge.app.subprocess.Popen',return_value=proc), patch('bridge.app.threading.Thread'):
                result=b.wake_console({'console':'40:D2:8A:43:4E:C8'})
            self.assertIn('already awake',result)
            self.assertEqual(b.last_wake_request,0)
            self.assertFalse((Path(d)/'wake-state.json').exists())

if __name__=='__main__':unittest.main()
