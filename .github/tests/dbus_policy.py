"""Exercise actual private-system-bus callback policy in the packaged image."""
import threading
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
daemon = dbus.SystemBus(private=True)
client = dbus.SystemBus(private=True)
name = dbus.service.BusName('org.bluez', bus=daemon)
called = set()

class Callbacks(dbus.service.Object):
    @dbus.service.method('org.bluez.Agent1', in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        called.add('Agent1')

    @dbus.service.method('org.bluez.Profile1', in_signature='', out_signature='')
    def Release(self):
        called.add('Profile1')

callbacks = Callbacks(client, '/bridge/policy_test')
loop = GLib.MainLoop()
thread = threading.Thread(target=loop.run, daemon=True)
thread.start()
try:
    remote = daemon.get_object(client.get_unique_name(), '/bridge/policy_test', introspect=False)
    dbus.Interface(remote, 'org.bluez.Agent1').RequestConfirmation(
        dbus.ObjectPath('/org/bluez/hci1/dev_00_11_22_33_44_55'), dbus.UInt32(0), timeout=5)
    dbus.Interface(remote, 'org.bluez.Profile1').Release(timeout=5)
    assert called == {'Agent1', 'Profile1'}, called
    print('Private D-Bus Agent1 and Profile1 callbacks verified')
finally:
    loop.quit()
    thread.join(timeout=2)
    callbacks.remove_from_connection()
    daemon.close()
    client.close()
