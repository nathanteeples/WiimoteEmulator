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
