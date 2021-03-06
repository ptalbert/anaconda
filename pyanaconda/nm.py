# Network configuration proxy to NetworkManager
#
# Copyright (C) 2013,2017  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#

import gi
gi.require_version("Gio", "2.0")
gi.require_version("NM", "1.0")

from gi.repository import Gio
from gi.repository import NM
from pyanaconda.core.glib import GError, VariantType
import struct
import socket

from pyanaconda.anaconda_loggers import get_module_logger
log = get_module_logger(__name__)

from pyanaconda.core.configuration.anaconda import conf

supported_device_types = [
    NM.DeviceType.ETHERNET,
    NM.DeviceType.WIFI,
    NM.DeviceType.INFINIBAND,
    NM.DeviceType.BOND,
    NM.DeviceType.VLAN,
    NM.DeviceType.BRIDGE,
    NM.DeviceType.TEAM,
]

DEFAULT_PROXY_FLAGS = \
    Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES

class UnknownDeviceError(ValueError):
    """Device of specified name was not found by NM"""
    def __str__(self):
        return self.__repr__()

class PropertyNotFoundError(ValueError):
    """Property of NM object was not found"""
    def __str__(self):
        return self.__repr__()

class SettingsNotFoundError(ValueError):
    """Settings NMRemoteConnection object was not found"""
    def __str__(self):
        return self.__repr__()

class UnknownMethodGetError(Exception):
    """Object does not have Get, most probably being invalid"""
    def __str__(self):
        return self.__repr__()

def _get_proxy(bus_type=Gio.BusType.SYSTEM,
               proxy_flags=DEFAULT_PROXY_FLAGS,
               info=None,
               name="org.freedesktop.NetworkManager",
               object_path="/org/freedesktop/NetworkManager",
               interface_name="org.freedesktop.NetworkManager",
               cancellable=None):
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(bus_type,
                                               proxy_flags,
                                               info,
                                               name,
                                               object_path,
                                               interface_name,
                                               cancellable)
    except GError as e:
        if conf.system.provides_system_bus:
            raise

        log.error("_get_proxy failed: %s", e)
        proxy = None

    return proxy

def _get_property(object_path, prop, interface_name_suffix=""):
    interface_name = "org.freedesktop.NetworkManager" + interface_name_suffix
    proxy = _get_proxy(object_path=object_path, interface_name="org.freedesktop.DBus.Properties")
    if not proxy:
        return None

    try:
        prop = proxy.Get('(ss)', interface_name, prop)
    except GError as e:
        if ("org.freedesktop.DBus.Error.AccessDenied" in e.message or
            "org.freedesktop.DBus.Error.InvalidArgs" in e.message):
            return None
        elif "org.freedesktop.DBus.Error.UnknownMethod" in e.message:
            raise UnknownMethodGetError
        else:
            raise

    return prop

def nm_state():
    """Return state of NetworkManager

    :return: state of NetworkManager
    :rtype: integer
    """
    prop = _get_property("/org/freedesktop/NetworkManager", "State")

    # If this is an image/dir install assume the network is up
    if not prop and not conf.target.is_hardware:
        return NM.State.CONNECTED_GLOBAL
    else:
        return prop

def nm_devices():
    """Return names of network devices supported in installer.

    :return: names of network devices supported in installer
    :rtype: list of strings
    """

    interfaces = []

    proxy = _get_proxy()
    if not proxy:
        return []

    devices = proxy.GetDevices()
    for device in devices:
        device_type = _get_property(device, "DeviceType", ".Device")
        if device_type not in supported_device_types:
            continue
        iface = _get_property(device, "Interface", ".Device")
        interfaces.append(iface)

    return interfaces

def nm_activated_devices():
    """Return names of activated network devices.

    :return: names of activated network devices
    :rtype: list of strings
    """

    interfaces = []

    active_connections = _get_property("/org/freedesktop/NetworkManager", "ActiveConnections")
    if not active_connections:
        return []

    for ac in active_connections:
        try:
            state = _get_property(ac, "State", ".Connection.Active")
        except UnknownMethodGetError:
            continue
        if state != NM.ActiveConnectionState.ACTIVATED:
            continue
        devices = _get_property(ac, "Devices", ".Connection.Active")
        for device in devices:
            iface = _get_property(device, "IpInterface", ".Device")
            if not iface:
                iface = _get_property(device, "Interface", ".Device")
            interfaces.append(iface)

    return interfaces

def _get_object_iface_names(object_path):
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    res_xml = connection.call_sync("org.freedesktop.NetworkManager",
                                   object_path,
                                   "org.freedesktop.DBus.Introspectable",
                                   "Introspect",
                                   None,
                                   VariantType.new("(s)"),
                                   Gio.DBusCallFlags.NONE,
                                   -1,
                                   None)
    node_info = Gio.DBusNodeInfo.new_for_xml(res_xml[0])
    return [iface.name for iface in node_info.interfaces]

def _device_type_specific_interface(device):
    ifaces = _get_object_iface_names(device)
    for iface in ifaces:
        if iface.startswith("org.freedesktop.NetworkManager.Device.") \
           and iface != "org.freedesktop.NetworkManager.Device.Statistics":
            return iface
    return None

def nm_device_property(name, prop):
    """Return value of device NM property

       :param name: name of device
       :type name: str
       :param prop: property
       :type name: str
       :return: value of device's property
       :rtype: unpacked GDBus value
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if property is not found
    """

    retval = None

    proxy = _get_proxy()
    try:
        device = proxy.GetDeviceByIpIface('(s)', name)
    except GError as e:
        if "org.freedesktop.NetworkManager.UnknownDevice" in e.message:
            raise UnknownDeviceError(name, e)
        raise

    retval = _get_property(device, prop, ".Device")
    if not retval:
        # Look in device type based interface
        interface = _device_type_specific_interface(device)
        if interface:
            retval = _get_property(device, prop, interface[30:])
            if not retval:
                raise PropertyNotFoundError(prop)
        else:
            raise PropertyNotFoundError(prop)

    return retval

def nm_device_type_is_wifi(name):
    """Is the type of device wifi?

       :param name: name of device
       :type name: str
       :return: True if type of device is WIFI, False otherwise
       :rtype: bool
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if property is not found
    """
    return nm_device_type(name) == NM.DeviceType.WIFI

def nm_device_type_is_ethernet(name):
    """Is the type of device ethernet?

       :param name: name of device
       :type name: str
       :return: True if type of device is ETHERNET, False otherwise
       :rtype: bool
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if property is not found
    """
    return nm_device_type(name) == NM.DeviceType.ETHERNET

def nm_device_type_is_team(name):
    """Is the type of device team?

       :param name: name of device
       :type name: str
       :return: True if type of device is TEAM, False otherwise
       :rtype: bool
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if property is not found
    """
    return nm_device_type(name) == NM.DeviceType.TEAM

def nm_device_hwaddress(name):
    """Return active hardware address of device ('HwAddress' property)

       :param name: name of device
       :type name: str
       :return: active hardware address of device ('HwAddress' property)
       :rtype: str
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if 'HwAddress' property is not found
    """
    return nm_device_property(name, "HwAddress")

def nm_device_perm_hwaddress(name):
    """Return active hardware address of device ('PermHwAddress' property)

       :param name: name of device
       :type name: str
       :return: active hardware address of device ('PermHwAddress' property)
       :rtype: str
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if 'PermHwAddress' property is not found
    """
    return nm_device_property(name, "PermHwAddress")

def nm_device_valid_hwaddress(name):
    """Return valid hardware address of device depending on type of the device
       ('PermHwAddress' property for wired and wireless or 'HwAddress' property for others)

       :param name: name of device
       :type name: str
       :return: active hardware address of device
                ('HwAddress' or 'PermHwAddress' property)
       :rtype: str
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if property is not found
    """
    if nm_device_type_is_ethernet(name) or nm_device_type_is_wifi(name):
        try:
            return nm_device_perm_hwaddress(name)
        except PropertyNotFoundError:
            # TODO: Remove this if everything will work well
            # fallback solution
            log.warning("Device %s don't have property PermHwAddress", name)
            return nm_device_hwaddress(name)
    else:
        return nm_device_hwaddress(name)

def nm_device_type(name):
    """Return device's type ('DeviceType' property).

       :param name: name of device
       :type name: str
       :return: device type
       :rtype: integer
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if 'DeviceType' property is not found
    """
    return nm_device_property(name, "DeviceType")

def nm_device_ip_addresses(name, version=4):
    """Return IP addresses of device in ACTIVATED state.

       :param name: name of device
       :type name: str
       :param version: version of IP protocol (value 4 or 6)
       :type version: int
       :return: IP addresses of device, empty list if device is not
                in ACTIVATED state
       :rtype: list of strings
       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if IP configuration is not found
    """
    retval = []
    config = nm_device_ip_config(name, version)
    if config:
        retval = [addrs[0] for addrs in config[0]]

    return retval

def nm_device_ip_config(name, version=4):
    """Return IP configurations of device in ACTIVATED state.

       :param name: name of device
       :type name: str
       :param version: version of IP protocol (value 4 or 6)
       :type version: int
       :return: IP configuration of device, empty list if device is not
                in ACTIVATED state
       :rtype:
               | [[[address1, prefix1, gateway1], [address2, prefix2, gateway2], ...],
               | [nameserver1, nameserver2]]
               | addressX, gatewayX: string
               | prefixX: int

       :raise UnknownDeviceError: if device is not found
       :raise PropertyNotFoundError: if ip configuration is not found
    """
    state = nm_device_property(name, "State")
    if state != NM.DeviceState.ACTIVATED:
        return []

    if version == 4:
        dbus_iface = ".IP4Config"
        prop = "Ip4Config"
    elif version == 6:
        dbus_iface = ".IP6Config"
        prop = "Ip6Config"
    else:
        return []

    config = nm_device_property(name, prop)
    if config == "/":
        return []

    try:
        addresses = _get_property(config, "Addresses", dbus_iface)
    # object is valid only if device is in ACTIVATED state (racy)
    except UnknownMethodGetError:
        return []

    addr_list = []
    for addr, prefix, gateway in addresses:
        # NOTE: There is an ipaddress for IP validation but
        # byte order of dbus value would need to be switched
        if version == 4:
            addr_str = nm_dbus_int_to_ipv4(addr)
            gateway_str = nm_dbus_int_to_ipv4(gateway)
        elif version == 6:
            addr_str = nm_dbus_ay_to_ipv6(addr)
            gateway_str = nm_dbus_ay_to_ipv6(gateway)
        addr_list.append([addr_str, prefix, gateway_str])

    try:
        nameservers = _get_property(config, "Nameservers", dbus_iface)
    # object is valid only if device is in ACTIVATED state (racy)
    except UnknownMethodGetError:
        return []

    ns_list = []
    for ns in nameservers:
        # TODO - look for a library function
        if version == 4:
            ns_str = nm_dbus_int_to_ipv4(ns)
        elif version == 6:
            ns_str = nm_dbus_ay_to_ipv6(ns)
        ns_list.append(ns_str)

    return [addr_list, ns_list]

def nm_ntp_servers_from_dhcp():
    """Return NTP servers obtained by DHCP.

       return: NTP servers obtained by DHCP
       rtype: list of str
    """
    ntp_servers = []
    # get paths for all actively connected interfaces
    active_devices = nm_activated_devices()
    for device in active_devices:
        # harvest NTP server addresses from DHCPv4
        dhcp4_path = nm_device_property(device, "Dhcp4Config")
        try:
            options = _get_property(dhcp4_path, "Options", ".DHCP4Config")
        # object is valid only if device is in ACTIVATED state (racy)
        except UnknownMethodGetError:
            options = None
        if options and 'ntp_servers' in options:
            # NTP server addresses returned by DHCP are whitespace delimited
            ntp_servers_string = options["ntp_servers"]
            for ip in ntp_servers_string.split(" "):
                ntp_servers.append(ip)

        # NetworkManager does not request NTP/SNTP options for DHCP6
    return ntp_servers

def _find_settings(value, key1, key2, format_value=lambda x: x):
    """Return list of object paths of settings having given value of key1, key2 setting

       :param value: required value of setting
       :type value: corresponds to dbus type of setting
       :param key1: first-level key of setting (eg "connection")
       :type key1: str
       :param key2: second-level key of setting (eg "uuid")
       :type key2: str
       :param format_value: function to be called on setting value before
                            comparing
       :type format_value: function taking one argument (setting value)
       :return: list of paths of settings
       :rtype: list
    """
    retval = []

    proxy = _get_proxy(object_path="/org/freedesktop/NetworkManager/Settings", interface_name="org.freedesktop.NetworkManager.Settings")

    connections = proxy.ListConnections()
    for con in connections:
        proxy = _get_proxy(object_path=con, interface_name="org.freedesktop.NetworkManager.Settings.Connection")
        try:
            settings = proxy.GetSettings()
        except GError as e:
            log.debug("Exception raised in _find_settings: %s", e)
            continue
        try:
            v = settings[key1][key2]
        except KeyError:
            continue
        if format_value(v) == value:
            retval.append(con)

    return retval

def nm_get_all_settings():
    """Return all settings for logging."""
    retval = []

    proxy = _get_proxy(object_path="/org/freedesktop/NetworkManager/Settings", interface_name="org.freedesktop.NetworkManager.Settings")

    connections = proxy.ListConnections()
    for con in connections:
        proxy = _get_proxy(object_path=con, interface_name="org.freedesktop.NetworkManager.Settings.Connection")
        try:
            settings = proxy.GetSettings()
        except GError as e:
            # The connection may be deleted asynchronously by NM
            log.debug("Exception raised in nm_get_all_settings: %s", e)
            continue
        retval.append(settings)

    return retval

def nm_ipv6_to_dbus_ay(address):
    """Convert ipv6 address from string to list of bytes 'ay' for dbus

    :param address: IPv6 address
    :type address: str
    :return: address in format 'ay' for NM dbus setting
    :rtype: list of bytes
    """
    return [int(byte) for byte in bytearray(socket.inet_pton(socket.AF_INET6, address))]

def nm_ipv4_to_dbus_int(address):
    """Convert ipv4 address from string to int for dbus (switched endianess).

    :param address: IPv4 address
    :type address: str
    :return: IPv4 address as an integer 'u' for NM dbus setting
    :rtype: integer
    """
    return struct.unpack("=L", socket.inet_aton(address))[0]


def nm_dbus_ay_to_ipv6(bytelist):
    """Convert ipv6 address from list of bytes (dbus 'ay') to string.

    :param address: IPv6 address as list of bytes returned by dbus ('ay')
    :type address: list of bytes - dbus 'ay'
    :return: IPv6 address
    :rtype: str
    """
    return socket.inet_ntop(socket.AF_INET6, bytes(bytelist))

def nm_dbus_int_to_ipv4(address):
    """Convert ipv4 address from dus int 'u' (switched endianess) to string.

    :param address: IPv4 address as integer returned by dbus ('u')
    :type address: integer - dbus 'u'
    :return: IPv6 address
    :rtype: str
    """
    return socket.inet_ntop(socket.AF_INET, struct.pack('=L', address))

def test():
    print("NM state: %s:" % nm_state())

    print("Devices: %s" % nm_devices())
    print("Activated devices: %s" % nm_activated_devices())

    devs = nm_devices()
    devs.append("nonexisting")
    for devname in devs:

        print(devname)

        try:
            devtype = nm_device_type(devname)
        except UnknownDeviceError as e:
            print("     %s" % e)
            devtype = None
        if devtype == NM.DeviceType.ETHERNET:
            print("     type %s" % "ETHERNET")
        elif devtype == NM.DeviceType.WIFI:
            print("     type %s" % "WIFI")

        try:
            print("     Wifi device: %s" % nm_device_type_is_wifi(devname))
        except UnknownDeviceError as e:
            print("     %s" % e)

        try:
            hwaddr = nm_device_hwaddress(devname)
            print("     HwAaddress: %s" % hwaddr)
        except ValueError as e:
            print("     %s" % e)
            hwaddr = ""

        try:
            print("     IP4 config: %s" % nm_device_ip_config(devname))
            print("     IP6 config: %s" % nm_device_ip_config(devname, version=6))
            print("     IP4 addrs: %s" % nm_device_ip_addresses(devname))
            print("     IP6 addrs: %s" % nm_device_ip_addresses(devname, version=6))
            print("     Udi: %s" % nm_device_property(devname, "Udi"))
        except UnknownDeviceError as e:
            print("     %s" % e)

        if devname in nm_devices():
            try:
                print("     Nonexisting: %s" % nm_device_property(devname, "Nonexisting"))
            except PropertyNotFoundError as e:
                print("     %s" % e)
        try:
            print("     Nonexisting: %s" % nm_device_property(devname, "Nonexisting"))
        except ValueError as e:
            print("     %s" % e)

if __name__ == "__main__":
    test()
