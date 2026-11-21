"""Compatibility limits for joycontrol on Switch firmware 12 and later."""
import asyncio
import time


def rate_limited_protocol(base):
    class SwitchProtocol(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._report_lock = asyncio.Lock()
            self._last_report = None

        async def write(self, report):
            # Upstream's 66 Hz sender is rejected in pairing and HOME menus.
            # Serialize startup reports and subcommand replies with state reports.
            async with self._report_lock:
                if self._last_report is not None:
                    delay = 1 / 15 - (time.monotonic() - self._last_report)
                    if delay > 0:
                        await asyncio.sleep(delay)
                await super().write(report)
                self._last_report = time.monotonic()
    return SwitchProtocol
