"""Automatic BlueZ BLE pairing with a NoInputNoOutput Agent1 via dbus-fast."""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/wiim_voice_remote/agent"


async def async_bluez_pair_and_trust(mac_address: str) -> bool:
    """Pair and trust a BLE device directly via BlueZ D-Bus using a NoInputNoOutput agent.

    Returns True if the device is paired (or was already paired), False otherwise.
    Gracefully returns False if D-Bus / BlueZ is not available (e.g. ESPHome proxy only).
    """
    try:
        from dbus_fast import BusType, Variant
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, method
    except ImportError:
        _LOGGER.debug("dbus_fast not available; skipping direct BlueZ agent pairing")
        return False

    class _NoInputNoOutputAgent(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self) -> None:
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821
            pass

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
            return "0000"

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
            return 0

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
            pass

        @method()
        def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
            pass

        @method()
        def Cancel(self) -> None:
            pass

    mac_suffix = f"/dev_{mac_address.upper().replace(':', '_')}"
    bus = None
    agent_mgr = None
    agent_registered = False

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        # Find the device path across any hci* adapter
        root_intro = await bus.introspect("org.bluez", "/")
        root_proxy = bus.get_proxy_object("org.bluez", "/", root_intro)
        obj_mgr = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await obj_mgr.call_get_managed_objects()

        dev_path: str | None = None
        already_paired = False
        for path, ifaces in managed.items():
            if path.endswith(mac_suffix) and "org.bluez.Device1" in ifaces:
                dev_path = path
                dev_props = ifaces["org.bluez.Device1"]
                paired_v = dev_props.get("Paired")
                already_paired = bool(paired_v.value) if paired_v else False
                break

        if not dev_path:
            _LOGGER.debug("Device %s not found in local BlueZ ObjectManager", mac_address)
            return False

        dev_intro = await bus.introspect("org.bluez", dev_path)
        dev_proxy = bus.get_proxy_object("org.bluez", dev_path, dev_intro)
        dev_iface = dev_proxy.get_interface("org.bluez.Device1")
        props_iface = dev_proxy.get_interface("org.freedesktop.DBus.Properties")

        if already_paired:
            _LOGGER.debug("Device %s is already paired in BlueZ", mac_address)
            try:
                await props_iface.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
            except Exception:
                pass
            return True

        # Register NoInputNoOutput agent so BlueZ accepts JustWorks BLE HID bonding
        agent = _NoInputNoOutputAgent()
        bus.export(AGENT_PATH, agent)
        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_proxy = bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
        agent_mgr = bluez_proxy.get_interface("org.bluez.AgentManager1")
        await agent_mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        agent_registered = True
        try:
            await agent_mgr.call_request_default_agent(AGENT_PATH)
        except Exception:
            pass

        _LOGGER.info("Pairing WiiM Voice Remote %s via BlueZ NoInputNoOutput agent...", mac_address)
        try:
            await dev_iface.call_pair()
        except Exception as pair_err:
            if "AlreadyExists" not in str(pair_err):
                _LOGGER.warning("BlueZ Pair() failed for %s: %s", mac_address, pair_err)
                return False

        await props_iface.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
        _LOGGER.info("Successfully paired and trusted WiiM Voice Remote %s in BlueZ", mac_address)
        return True
    except Exception as err:
        _LOGGER.debug("BlueZ direct pairing skipped/failed for %s: %s", mac_address, err)
        return False
    finally:
        if agent_registered and agent_mgr is not None:
            try:
                await agent_mgr.call_unregister_agent(AGENT_PATH)
            except Exception:
                pass
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:
                pass
