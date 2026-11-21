"""Joycontrol Pro Controller session, bound exclusively to the configured adapter.

Protocol implementation: mart1nro/joycontrol (GPL-3.0). Keep HOME asserted in the
first reports of reconnect, rather than waiting for a sleeping console's reply.
"""
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading


def event(name, **fields):
    print('BRIDGE_EVENT ' + json.dumps(dict(event=name, **fields)), flush=True)


async def run(action, address):
    # Import under the running loop: upstream utils captures its default loop.
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    from joycontrol.controller import Controller
    from joycontrol.controller_state import button_push
    from joycontrol.device import HidDevice
    from joycontrol.memory import FlashMemory
    from joycontrol.protocol import controller_protocol_factory
    from joycontrol.report import InputReport
    from joycontrol.transport import L2CAP_Transport
    import joycontrol

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    pairing = action == 'pair'
    expected = address.upper() if address != '-' else None

    class Agent(dbus.service.Object):
        @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
        def Release(self):
            pass

        @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
        def Cancel(self):
            pass

        def check(self, device):
            peer = str(device).rsplit('/dev_', 1)[-1].replace('_', ':').upper()
            if not pairing and peer != expected:
                raise dbus.exceptions.DBusException('Unexpected console', name='org.bluez.Error.Rejected')

        @dbus.service.method('org.bluez.Agent1', in_signature='ou', out_signature='')
        def RequestConfirmation(self, device, passkey):
            self.check(device)

        @dbus.service.method('org.bluez.Agent1', in_signature='os', out_signature='')
        def AuthorizeService(self, device, uuid):
            self.check(device)

        @dbus.service.method('org.bluez.Agent1', in_signature='o', out_signature='')
        def RequestAuthorization(self, device):
            self.check(device)

    agent = Agent(bus, '/bridge/agent')
    manager = dbus.Interface(bus.get_object('org.bluez', '/org/bluez'), 'org.bluez.AgentManager1')
    manager.RegisterAgent('/bridge/agent', 'NoInputNoOutput')
    manager.RequestDefaultAgent('/bridge/agent')
    glib = GLib.MainLoop()
    glib_thread = threading.Thread(target=glib.run, daemon=True)
    glib_thread.start()
    hid = HidDevice(device_id=os.getenv('HCI_DEVICE', 'hci0'))
    protocol = controller_protocol_factory(Controller.PRO_CONTROLLER, spi_flash=FlashMemory())()
    state = protocol.get_controller_state()
    if not pairing:
        state.button_state.set_button('home', True)
    sockets, transport, starter = [], None, None
    loop = asyncio.get_running_loop()

    def channel():
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        sockets.append(sock)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock

    try:
        hid.powered(True)
        await hid.set_name(protocol.controller.device_name())
        await hid.set_class()
        record = Path(joycontrol.__file__).parent / 'profile' / 'sdp_record_hid.xml'
        HidDevice.register_sdp_record(str(record))
        if pairing:
            control, interrupt = channel(), channel()
            control.bind((hid.address, 17))
            interrupt.bind((hid.address, 19))
            control.listen(1)
            interrupt.listen(1)
            hid.pairable(True)
            hid.discoverable(True)
            print('Open Switch Controllers → Change Grip/Order to pair', flush=True)
            ctl, ctl_peer = await loop.sock_accept(control)
            sockets.append(ctl)
            itr, itr_peer = await loop.sock_accept(interrupt)
            sockets.append(itr)
            if ctl_peer[0] != itr_peer[0]:
                raise RuntimeError('HID channels came from different consoles; retry pairing')
            address = ctl_peer[0].upper()
            hid.discoverable(False)
            hid.pairable(False)
        else:
            ctl, itr = channel(), channel()
            # Upstream reconnect does not bind; explicitly avoid the host Intel radio.
            ctl.bind((hid.address, 0))
            itr.bind((hid.address, 0))
            await asyncio.wait_for(loop.sock_connect(ctl, (address, 17)), 15)
            await asyncio.wait_for(loop.sock_connect(itr, (address, 19)), 15)
        transport = L2CAP_Transport(loop, protocol, itr, ctl, 50)
        protocol.connection_made(transport)

        async def initial_reports():
            report = InputReport()
            report.set_input_report_id(0x30)
            report.set_misc()
            report.set_vibrator_input()
            for _ in range(20):
                await protocol.write(report)
                await asyncio.sleep(.1)

        starter = asyncio.create_task(initial_reports())
        await asyncio.wait_for(protocol.wait_for_output_report(), 12)
        starter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await starter
        state.button_state.set_button('home', False)
        await asyncio.wait_for(state.connect(), 15)
        if pairing:
            event('paired', address=address)
        else:
            await button_push(state, 'home', sec=.15)
            event('home', address=address)
        # Warm connection: additional HOME commands do not repeat pairing/handshake.
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line or line.strip() == 'close':
                break
            if line.strip() == 'home':
                await button_push(state, 'home', sec=.15)
                event('home', address=address)
    finally:
        if starter:
            starter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await starter
        if transport:
            with contextlib.suppress(Exception):
                await transport.close()
        for sock in sockets:
            sock.close()
        with contextlib.suppress(Exception):
            hid.discoverable(False)
            hid.pairable(False)
            manager.UnregisterAgent('/bridge/agent')
        agent.remove_from_connection()
        glib.quit()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        event('error', message=str(exc))
        raise SystemExit(1)
