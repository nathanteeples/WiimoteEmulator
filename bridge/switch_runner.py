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
import signal
import sys
import threading


def event(name, **fields):
    print('BRIDGE_EVENT ' + json.dumps(dict(event=name, **fields)), flush=True)


async def wait_stage(awaitable, timeout, description):
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError('Switch timed out ' + description) from exc


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
    from joycontrol.protocol import ControllerProtocol
    from bridge.switch_protocol import rate_limited_protocol
    from bridge.bluetooth_events import PairingMonitor
    from bridge.switch_dbus import set_adapter_property
    from joycontrol.report import InputReport
    from joycontrol.transport import L2CAP_Transport
    import joycontrol

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
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
            if not str(device).startswith('/org/bluez/' + hid._adapter_name + '/dev_'):
                raise dbus.exceptions.DBusException('Unexpected adapter', name='org.bluez.Error.Rejected')
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

    hid = HidDevice(device_id=os.getenv('HCI_DEVICE', 'hci0'))
    agent = Agent(bus, '/bridge/agent')
    manager = dbus.Interface(bus.get_object('org.bluez', '/org/bluez'), 'org.bluez.AgentManager1')
    manager.RegisterAgent('/bridge/agent', 'NoInputNoOutput')
    manager.RequestDefaultAgent('/bridge/agent')
    glib = GLib.MainLoop()
    glib_thread = threading.Thread(target=glib.run, daemon=True)
    glib_thread.start()
    protocol = rate_limited_protocol(ControllerProtocol)(Controller.PRO_CONTROLLER, spi_flash=FlashMemory())
    state = protocol.get_controller_state()
    if not pairing:
        state.button_state.set_button('home', True)
    sockets, transport, starter = [], None, None
    monitor = PairingMonitor(loop, hid._adapter_name)
    loop = asyncio.get_running_loop()

    def channel():
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        sockets.append(sock)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock

    async def gamepad_class():
        # HCI-only writes leave the kernel/BlueZ class cached as "Computer".
        # Update MGMT first so subsequent SDP/EIR updates preserve Gamepad.
        process = await asyncio.create_subprocess_exec('btmgmt', '--index', hid._adapter_name,
            'class', '5', '8', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, error = await process.communicate()
        if process.returncode:
            raise RuntimeError('Unable to set Bluetooth gamepad class: ' + error.decode().strip())
        await hid.set_class()
        for _ in range(20):
            cached = int(hid.properties.Get('org.bluez.Adapter1', 'Class'))
            if cached & 0x1ffc == 0x0508:
                print('Gamepad identity verified in BlueZ and HCI', flush=True)
                return
            await asyncio.sleep(.05)
        raise RuntimeError('BlueZ is overriding the Gamepad class; pairing stopped')

    try:
        set_adapter_property(hid.properties, 'Powered', True)
        set_adapter_property(hid.properties, 'Alias', protocol.controller.device_name())
        record = Path(joycontrol.__file__).parent / 'profile' / 'sdp_record_hid.xml'
        HidDevice.register_sdp_record(str(record))
        if pairing:
            control, interrupt = channel(), channel()
            control.bind((hid.address, 17))
            interrupt.bind((hid.address, 19))
            control.listen(1)
            interrupt.listen(1)
            set_adapter_property(hid.properties, 'Pairable', True)
            set_adapter_property(hid.properties, 'Discoverable', True)
            # Discoverability and SDP changes make BlueZ rewrite device class.
            await gamepad_class()
            print('Switch advertisement ready: Gamepad 0x002508 on ' + hid._adapter_name, flush=True)
            print('Open Switch Controllers → Change Grip/Order to pair', flush=True)
            ctl, ctl_peer = await loop.sock_accept(control)
            sockets.append(ctl)
            print('Switch HID control connected: ' + ctl_peer[0], flush=True)
            itr, itr_peer = await loop.sock_accept(interrupt)
            sockets.append(itr)
            print('Switch HID interrupt connected: ' + itr_peer[0], flush=True)
            if ctl_peer[0] != itr_peer[0]:
                raise RuntimeError('HID channels came from different consoles; retry pairing')
            address = ctl_peer[0].upper()
            set_adapter_property(hid.properties, 'Discoverable', False)
            set_adapter_property(hid.properties, 'Pairable', False)
        else:
            await gamepad_class()
            ctl, itr = channel(), channel()
            # Upstream reconnect does not bind; explicitly avoid the host Intel radio.
            ctl.bind((hid.address, 0))
            itr.bind((hid.address, 0))
            await wait_stage(loop.sock_connect(ctl, (address, 17)), 15, 'connecting the Bluetooth HID control channel; check the console is awake and paired')
            await wait_stage(loop.sock_connect(itr, (address, 19)), 15, 'connecting the Bluetooth HID interrupt channel')
        transport = L2CAP_Transport(loop, protocol, itr, ctl, 50)
        protocol.connection_made(transport)

        async def initial_reports():
            report = InputReport()
            # Match joycontrol's empty startup report; v12+ firmware expects
            # one per second until it begins its controller inquiry.
            for _ in range(20):
                await protocol.write(report)
                await asyncio.sleep(1)

        starter = asyncio.create_task(initial_reports())
        await wait_stage(protocol.wait_for_output_report(), 12, 'waiting for the first controller inquiry after HID connected')
        starter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await starter
        state.button_state.set_button('home', False)
        await wait_stage(state.connect(), 15, 'waiting for controller/player assignment after HID connected')
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
        monitor.close()
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
            set_adapter_property(hid.properties, 'Discoverable', False)
            set_adapter_property(hid.properties, 'Pairable', False)
            manager.UnregisterAgent('/bridge/agent')
        agent.remove_from_connection()
        glib.quit()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        event('error', message=str(exc))
        raise SystemExit(1)
