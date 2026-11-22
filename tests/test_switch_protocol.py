import asyncio
import unittest
from unittest.mock import patch
from bridge.switch_protocol import rate_limited_protocol

class SwitchReports(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reports_keep_firmware_safe_rate_and_order(self):
        clock = [10.0]
        writes = []
        class Base:
            def __init__(self):
                pass
            async def write(self, report):
                writes.append((report, clock[0]))
        async def sleep(delay):
            clock[0] += delay
        with patch('bridge.switch_protocol.time.monotonic', side_effect=lambda: clock[0]), patch('bridge.switch_protocol.asyncio.sleep', side_effect=sleep):
            protocol = rate_limited_protocol(Base)()
            await asyncio.gather(*(protocol.write(i) for i in range(4)))
        self.assertEqual([v for v, t in writes], [0, 1, 2, 3])
        for previous, current in zip(writes, writes[1:]):
            self.assertGreaterEqual(current[1] - previous[1], 1/15 - 1e-8)

class PairingDiagnostics(unittest.TestCase):
    def test_authentication_failures_include_peer_and_status(self):
        from bridge.bluetooth_events import describe
        packet = bytes.fromhex('04 36 07 05 55 44 33 22 11 02')
        self.assertEqual(describe(packet), 'Bluetooth secure pairing FAILED: 02:11:22:33:44:55, status 0x05')

    def test_key_notifications_and_malformed_packets_are_ignored(self):
        from bridge.bluetooth_events import describe
        self.assertIsNone(describe(bytes([4, 0x18, 23]) + b'private key data'))
        self.assertIsNone(describe(bytes.fromhex('04 36 07 05')))


class WarmSwitchSession(unittest.TestCase):
    def test_live_connection_sends_home_without_spawning(self):
        from unittest.mock import Mock
        from bridge.bluetooth import SwitchSession
        session = SwitchSession(Mock())
        session.proc = Mock(); session.proc.poll.return_value = None
        session.address = '02:11:22:33:44:55'
        session.events.put({'event': 'home', 'address': session.address})
        with patch('bridge.bluetooth.subprocess.Popen') as spawn:
            self.assertEqual(session.command('on', session.address), session.address)
            spawn.assert_not_called()
        session.proc.stdin.write.assert_called_once_with('home\n')

    def test_expired_warm_connection_reconnects_once(self):
        from unittest.mock import Mock
        from bridge.bluetooth import SwitchSession
        session = SwitchSession(Mock())
        session.proc = Mock(); session.proc.poll.return_value = None
        session.address = address = '02:11:22:33:44:55'
        session.events.put({'event': 'error', 'message': 'Disconnected'})
        with patch('bridge.bluetooth.subprocess.Popen') as spawn, patch('bridge.bluetooth.threading.Thread') as reader:
            reader.return_value.start.side_effect = lambda: session.events.put({'event': 'home', 'address': address})
            self.assertEqual(session.command('on', address), address)
            spawn.assert_called_once()
            self.assertIn(address, spawn.call_args.args[0])
