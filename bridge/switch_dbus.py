"""Explicit D-Bus variants avoid upstream introspection races during reconnect."""


def set_adapter_property(properties, name, value):
    import dbus
    if type(value) is bool:
        value = dbus.Boolean(value, variant_level=1)
    elif isinstance(value, str):
        value = dbus.String(value, variant_level=1)
    else:
        raise TypeError('Unsupported adapter property type')
    properties.Set('org.bluez.Adapter1', name, value, signature='ssv')
