"""Automatic BlueZ BLE pairing with a NoInputNoOutput Agent1 via dbus-fast."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/wiim_voice_remote/agent"


class BlueZPairingSession:
    """Context manager that keeps a BlueZ NoInputNoOutput Agent1 registered across BLE connect + pair."""

    def __init__(self, mac_address: str) -> None:
        self.mac_address = mac_address.upper()
        self.mac_suffix = f"/dev_{self.mac_address.replace(':', '_')}"
        self.was_paired = False
        self.auth_rejected = False
        self._bus: Any = None
        self._agent_mgr: Any = None
        self._agent_registered = False

    async def __aenter__(self) -> BlueZPairingSession:
        try:
            from dbus_fast import BusType, Variant
            from dbus_fast.aio import MessageBus
            from dbus_fast.service import ServiceInterface, method
        except ImportError:
            _LOGGER.debug("dbus_fast not available; skipping direct BlueZ agent registration")
            return self

        try:
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
                def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821
                    pass

                @method()
                def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
                    return 0

                @method()
                def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821
                    pass

                @method()
                def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
                    pass

                @method()
                def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
                    pass

                @method()
                def Cancel(self) -> None:
                    pass

            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

            # Register NoInputNoOutput agent BEFORE connecting so BlueZ can handle both
            # peripheral-initiated SMP Security Requests and central-initiated Pair() calls.
            agent = _NoInputNoOutputAgent()
            self._bus.export(AGENT_PATH, agent)
            bluez_intro = await self._bus.introspect("org.bluez", "/org/bluez")
            bluez_proxy = self._bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
            self._agent_mgr = bluez_proxy.get_interface("org.bluez.AgentManager1")
            try:
                await self._agent_mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
                self._agent_registered = True
            except Exception as reg_err:
                if "AlreadyExists" in str(reg_err):
                    self._agent_registered = True
                else:
                    _LOGGER.debug("BlueZ RegisterAgent note: %s", reg_err)

            if self._agent_registered:
                try:
                    await self._agent_mgr.call_request_default_agent(AGENT_PATH)
                except Exception:
                    pass

            # Check initial Paired/Bonded state before connecting (do NOT call Pair() while disconnected)
            dev_path, is_paired = await self._async_get_device_state()
            self.was_paired = is_paired
            if dev_path:
                await self._async_set_trusted(dev_path, Variant)
        except Exception as err:
            _LOGGER.debug("BlueZ pairing session init note for %s: %s", self.mac_address, err)

        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._agent_registered and self._agent_mgr is not None:
            try:
                await self._agent_mgr.call_unregister_agent(AGENT_PATH)
            except Exception:
                pass
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
        self._bus = None
        self._agent_mgr = None
        self._agent_registered = False

    async def _async_get_device_state(self) -> tuple[Optional[str], bool]:
        """Return (dev_path, is_paired_or_bonded) from BlueZ ObjectManager."""
        if self._bus is None:
            return None, False
        root_intro = await self._bus.introspect("org.bluez", "/")
        root_proxy = self._bus.get_proxy_object("org.bluez", "/", root_intro)
        obj_mgr = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await obj_mgr.call_get_managed_objects()

        for path, ifaces in managed.items():
            if path.endswith(self.mac_suffix) and "org.bluez.Device1" in ifaces:
                dev_props = ifaces["org.bluez.Device1"]
                paired_v = dev_props.get("Paired")
                bonded_v = dev_props.get("Bonded")
                is_paired = bool(
                    (paired_v and paired_v.value) or (bonded_v and bonded_v.value)
                )
                return path, is_paired
        return None, False

    async def _async_set_trusted(self, dev_path: str, variant_cls: Any) -> None:
        if self._bus is None:
            return
        try:
            dev_intro = await self._bus.introspect("org.bluez", dev_path)
            dev_proxy = self._bus.get_proxy_object("org.bluez", dev_path, dev_intro)
            props_iface = dev_proxy.get_interface("org.freedesktop.DBus.Properties")
            await props_iface.call_set("org.bluez.Device1", "Trusted", variant_cls("b", True))
        except Exception:
            pass

    async def async_pair_and_trust(self, client: Any = None, force_pair: bool = False) -> bool:
        """Ensure the connected BLE device is paired and trusted in BlueZ while the agent is active."""
        try:
            from dbus_fast import Variant
        except ImportError:
            return False

        if self._bus is None:
            return False

        try:
            dev_path, is_paired = await self._async_get_device_state()
            if not dev_path:
                _LOGGER.debug("Device %s not found in local BlueZ ObjectManager", self.mac_address)
                return False

            await self._async_set_trusted(dev_path, Variant)

            if is_paired and not force_pair:
                self.was_paired = True
                _LOGGER.debug("Device %s is already paired in BlueZ", self.mac_address)
                return True

            dev_intro = await self._bus.introspect("org.bluez", dev_path)
            dev_proxy = self._bus.get_proxy_object("org.bluez", dev_path, dev_intro)
            dev_iface = dev_proxy.get_interface("org.bluez.Device1")

            _LOGGER.info(
                "Pairing WiiM Voice Remote %s via BlueZ NoInputNoOutput agent...",
                self.mac_address,
            )
            try:
                await dev_iface.call_pair()
            except Exception as pair_err:
                err_str = str(pair_err)
                if "AlreadyExists" in err_str:
                    pass
                elif "InProgress" in err_str:
                    # Peripheral already triggered SMP pairing upon connection; wait for completion
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        _, now_paired = await self._async_get_device_state()
                        if now_paired:
                            break
                    else:
                        _LOGGER.warning("BlueZ in-progress Pair() timed out for %s", self.mac_address)
                        return False
                else:
                    if "Authentication" in err_str:
                        self.auth_rejected = True
                    _LOGGER.warning("BlueZ Pair() failed for %s: %s", self.mac_address, pair_err)
                    return False

            await self._async_set_trusted(dev_path, Variant)
            _LOGGER.info("Successfully paired and trusted WiiM Voice Remote %s in BlueZ", self.mac_address)
            return True
        except Exception as err:
            _LOGGER.debug("BlueZ pairing failed for %s: %s", self.mac_address, err)
            return False


async def async_bluez_pair_and_trust(mac_address: str, client: Any = None) -> bool:
    """Pair and trust a BLE device via BlueZ D-Bus using a NoInputNoOutput agent."""
    async with BlueZPairingSession(mac_address) as session:
        return await session.async_pair_and_trust(client=client)


async def async_bluez_remove_device(mac_address: str) -> bool:
    """Remove a stale paired device from local BlueZ so it can re-pair cleanly."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:
        return False

    mac_suffix = f"/dev_{mac_address.upper().replace(':', '_')}"
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        root_intro = await bus.introspect("org.bluez", "/")
        root_proxy = bus.get_proxy_object("org.bluez", "/", root_intro)
        obj_mgr = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await obj_mgr.call_get_managed_objects()

        for path, ifaces in managed.items():
            if path.endswith(mac_suffix) and "org.bluez.Device1" in ifaces:
                dev_props = ifaces["org.bluez.Device1"]
                paired_v = dev_props.get("Paired")
                bonded_v = dev_props.get("Bonded")
                is_paired = bool(
                    (paired_v and paired_v.value) or (bonded_v and bonded_v.value)
                )
                if not is_paired:
                    _LOGGER.debug(
                        "Skipping BlueZ RemoveDevice for %s because it is not paired/bonded in BlueZ",
                        mac_address,
                    )
                    return False

                adapter_path = path.rsplit("/", 1)[0]
                ad_intro = await bus.introspect("org.bluez", adapter_path)
                ad_proxy = bus.get_proxy_object("org.bluez", adapter_path, ad_intro)
                ad_iface = ad_proxy.get_interface("org.bluez.Adapter1")
                await ad_iface.call_remove_device(path)
                _LOGGER.info("Removed stale BlueZ bond for %s (%s)", mac_address, path)
                return True
        return False
    except Exception as err:
        _LOGGER.debug("BlueZ RemoveDevice failed for %s: %s", mac_address, err)
        return False
    finally:
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:
                pass
