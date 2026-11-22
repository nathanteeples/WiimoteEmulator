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

class AdapterProperties(dbus.service.Object):
    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='ssv', out_signature='')
    def Set(self, interface, name, value):
        assert interface == 'org.bluez.Adapter1'
        assert value.variant_level == 1
        called.add(str(name))

callbacks = Callbacks(client, '/bridge/policy_test')
adapter = AdapterProperties(daemon, '/bridge/policy_adapter')
loop = GLib.MainLoop()
thread = threading.Thread(target=loop.run, daemon=True)
thread.start()
try:
    remote = daemon.get_object(client.get_unique_name(), '/bridge/policy_test', introspect=False)
    dbus.Interface(remote, 'org.bluez.Agent1').RequestConfirmation(
        dbus.ObjectPath('/org/bluez/hci1/dev_00_11_22_33_44_55'), dbus.UInt32(0), timeout=5)
    dbus.Interface(remote, 'org.bluez.Profile1').Release(timeout=5)
    from bridge.switch_dbus import set_adapter_property
    # Disable introspection to reproduce upstream's signature-guessing race.
    proxy = client.get_object('org.bluez', '/bridge/policy_adapter', introspect=False)
    properties = dbus.Interface(proxy, 'org.freedesktop.DBus.Properties')
    set_adapter_property(properties, 'Powered', True)
    set_adapter_property(properties, 'Alias', 'Pro Controller')
    assert called == {'Agent1', 'Profile1', 'Powered', 'Alias'}, called
    print('Private D-Bus callbacks and explicit adapter property variants verified')
finally:
    loop.quit()
    thread.join(timeout=2)
    callbacks.remove_from_connection()
    adapter.remove_from_connection()
    daemon.close()
    client.close()
