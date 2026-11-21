"""Read-only, narrowly filtered Bluetooth pairing diagnostics; never capture keys."""
import socket
import struct

# HCI event packet codes. Link-key notifications and command payloads are excluded.
EVENTS = (0x03, 0x04, 0x05, 0x06, 0x36)


def describe(packet):
    if len(packet) < 3 or packet[0] != 4 or packet[1] not in EVENTS:
        return None
    code, size = packet[1:3]
    data = packet[3:]
    if len(data) != size:
        return None
    address = lambda raw: ':'.join(f'{byte:02X}' for byte in reversed(raw))
    if code == 0x04 and size == 10:
        return 'Bluetooth connection requested by ' + address(data[:6])
    if code == 0x03 and size == 11:
        return f'Bluetooth ACL connection {address(data[3:9])}: status 0x{data[0]:02X}'
    if code == 0x36 and size == 7:
        result = 'succeeded' if data[0] == 0 else 'FAILED'
        return f'Bluetooth secure pairing {result}: {address(data[1:7])}, status 0x{data[0]:02X}'
    if code == 0x06 and size == 3 and data[0]:
        return f'Bluetooth authentication FAILED: status 0x{data[0]:02X}'
    if code == 0x05 and size == 4:
        return f'Bluetooth link disconnected: reason 0x{data[3]:02X}'
    return None


class PairingMonitor:
    def __init__(self, loop, adapter):
        self.loop, self.sock = loop, None
        try:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
            self.sock.bind((int(adapter.removeprefix('hci')),))
            self.sock.setblocking(False)
            low = sum(1 << event for event in EVENTS if event < 32)
            high = sum(1 << (event - 32) for event in EVENTS if event >= 32)
            self.sock.setsockopt(0, 2, struct.pack('IIIH', 1 << 4, low, high, 0) + b'\x00\x00')
            loop.add_reader(self.sock.fileno(), self.read)
        except (OSError, ValueError) as exc:
            self.close()
            print('Bluetooth pairing diagnostics unavailable: ' + str(exc), flush=True)

    def read(self):
        try:
            message = describe(self.sock.recv(260))
            if message:
                print(message, flush=True)
        except BlockingIOError:
            pass
        except OSError:
            self.close()

    def close(self):
        if self.sock:
            self.loop.remove_reader(self.sock.fileno())
            self.sock.close()
            self.sock = None
