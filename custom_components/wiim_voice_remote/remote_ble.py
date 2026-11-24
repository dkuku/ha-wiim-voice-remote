"""BLE client for the WiiM Voice Remote.

Primary transport on Linux / HAOS: direct BlueZ D-Bus via dbus-fast.
Why? Paired BLE HID remotes automatically reconnect to BlueZ when woken by a
button press without emitting connectable advertisements, so Bleak/habluetooth
scanners often miss them. Subscribing directly to BlueZ D-Bus PropertiesChanged
handles sleep/wake reconnects instantaneously with zero polling timeouts.

Fallback transport (when System D-Bus is unavailable): Home Assistant Bluetooth / Bleak.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .adpcm import decode_wiim_adpcm_packet
from .bluez_pair import async_bluez_pair_and_trust
from .const import (
    KBD_PRESETS,
    KEY_MUTE,
    KEY_NEXT_TRACK,
    KEY_PLAY_PAUSE,
    KEY_PREV_TRACK,
    KEY_RELEASE,
    KEY_VOICE_PTT,
    KEY_VOLUME_DOWN,
    KEY_VOLUME_UP,
    REPORT_REF_CONSUMER,
    REPORT_REF_KEYBOARD,
    REPORT_REF_VOICE_ADPCM,
    UUID_BATTERY_LEVEL,
    UUID_HID_REPORT,
    UUID_REPORT_REF,
)

_LOGGER = logging.getLogger(__name__)

CONSUMER_BUTTON_NAMES = {
    KEY_VOLUME_UP: "volume_up",
    KEY_VOLUME_DOWN: "volume_down",
    KEY_MUTE: "mute",
    KEY_PLAY_PAUSE: "play_pause",
    KEY_NEXT_TRACK: "next_track",
    KEY_PREV_TRACK: "previous_track",
}


class WiiMRemoteClient:
    """Maintains a BLE connection to a WiiM Voice Remote and dispatches voice/button/battery events."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        on_voice_start: Callable[[], None],
        on_voice_pcm: Callable[[bytes], None],
        on_voice_stop: Callable[[], None],
        on_button: Callable[[str], None],
        on_battery: Callable[[int], None] | None = None,
    ) -> None:
        self.hass = hass
        self.mac_address = mac_address.upper()
        self._mac_suffix = f"/dev_{self.mac_address.replace(':', '_')}"
        self._on_voice_start = on_voice_start
        self._on_voice_pcm = on_voice_pcm
        self._on_voice_stop = on_voice_stop
        self._on_button = on_button
        self._on_battery = on_battery

        self._client: BleakClient | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._unsub_bt: Callable[[], None] | None = None
        self._voice_active = False

        # BlueZ D-Bus characteristic paths
        self._dev_path: str | None = None
        self._consumer_path: str | None = None
        self._kbd_path: str | None = None
        self._voice_path: str | None = None
        self._battery_path: str | None = None

    def start(self) -> None:
        """Start background listener task."""
        if self._unsub_bt is None:
            self._unsub_bt = bluetooth.async_register_callback(
                self.hass,
                self._on_bt_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.mac_address,
                    connectable=True,
                ),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._wake_event.set()
            self._task = self.hass.async_create_background_task(
                self._run_main(),
                name=f"wiim_voice_remote_{self.mac_address}",
            )

    def _on_bt_advertisement(
        self,
        _service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        self._wake_event.set()

    async def async_stop(self) -> None:
        """Stop background task and disconnect."""
        if self._unsub_bt is not None:
            self._unsub_bt()
            self._unsub_bt = None
        self._stop_event.set()
        self._wake_event.set()
        self._disconnected_event.set()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---- Shared packet handlers ----

    def _process_consumer(self, raw: bytes) -> None:
        if raw == KEY_VOICE_PTT:
            if not self._voice_active:
                self._voice_active = True
                self._on_voice_start()
            return
        if raw == KEY_RELEASE:
            if self._voice_active:
                self._voice_active = False
                self._on_voice_stop()
            return

        btn = CONSUMER_BUTTON_NAMES.get(raw)
        if btn:
            self._on_button(btn)
        else:
            _LOGGER.debug("Unmapped consumer key from %s: %s", self.mac_address, raw.hex())

    def _process_keyboard(self, raw: bytes) -> None:
        if len(raw) < 3 or raw[2] == 0:
            return
        preset = KBD_PRESETS.get(raw[2])
        if preset is not None:
            self._on_button(f"preset_{preset}")
        else:
            _LOGGER.debug("Unmapped keyboard key from %s: 0x%02x", self.mac_address, raw[2])

    def _process_voice(self, raw: bytes) -> None:
        if not self._voice_active:
            self._voice_active = True
            self._on_voice_start()
        pcm = decode_wiim_adpcm_packet(raw)
        if pcm:
            self._on_voice_pcm(pcm)

    def _process_battery(self, raw: bytes) -> None:
        if raw and self._on_battery:
            level = max(0, min(100, int(raw[0])))
            self._on_battery(level)

    # ---- Main dispatcher (BlueZ D-Bus first, Bleak fallback) ----

    async def _run_main(self) -> None:
        try:
            await self._run_bluez_dbus()
            return
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug(
                "BlueZ D-Bus transport unavailable (%s); falling back to Bleak", err
            )

        await self._run_bleak_loop()

    # ---- Native BlueZ D-Bus transport ----

    async def _bluez_subscribe_all(self, bus) -> bool:
        """Discover HID/Battery characteristics in BlueZ and call StartNotify."""
        root_intro = await bus.introspect("org.bluez", "/")
        root_proxy = bus.get_proxy_object("org.bluez", "/", root_intro)
        obj_mgr = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await obj_mgr.call_get_managed_objects()

        dev_path: str | None = None
        connected = False
        resolved = False
        paired = False

        for path, ifaces in managed.items():
            if path.endswith(self._mac_suffix) and "org.bluez.Device1" in ifaces:
                dev_path = path
                props = ifaces["org.bluez.Device1"]
                connected = bool(props["Connected"].value) if "Connected" in props else False
                resolved = (
                    bool(props["ServicesResolved"].value)
                    if "ServicesResolved" in props
                    else False
                )
                paired = bool(props["Paired"].value) if "Paired" in props else False
                break

        if not dev_path:
            return False

        self._dev_path = dev_path

        if not paired:
            paired = await async_bluez_pair_and_trust(self.mac_address)
            if not paired:
                return False

        if not connected:
            try:
                dev_intro = await bus.introspect("org.bluez", dev_path)
                dev_proxy = bus.get_proxy_object("org.bluez", dev_path, dev_intro)
                dev_iface = dev_proxy.get_interface("org.bluez.Device1")
                await asyncio.wait_for(dev_iface.call_connect(), timeout=6.0)
                # Refresh managed objects after connect
                managed = await obj_mgr.call_get_managed_objects()
                props = managed.get(dev_path, {}).get("org.bluez.Device1", {})
                resolved = (
                    bool(props["ServicesResolved"].value)
                    if "ServicesResolved" in props
                    else False
                )
            except Exception as err:
                _LOGGER.debug("BlueZ Connect(%s) waiting for remote wake: %s", self.mac_address, err)
                return False

        if not resolved:
            return False

        self._consumer_path = None
        self._kbd_path = None
        self._voice_path = None
        self._battery_path = None

        for path, ifaces in managed.items():
            if not path.startswith(dev_path + "/"):
                continue
            char = ifaces.get("org.bluez.GattCharacteristic1")
            if not char:
                continue
            cuuid = str(char["UUID"].value).lower() if "UUID" in char else ""
            if cuuid == UUID_BATTERY_LEVEL:
                self._battery_path = path
                continue
            if cuuid != UUID_HID_REPORT:
                continue

            for dpath, difaces in managed.items():
                if not dpath.startswith(path + "/"):
                    continue
                desc = difaces.get("org.bluez.GattDescriptor1")
                if not desc:
                    continue
                duuid = str(desc["UUID"].value).lower() if "UUID" in desc else ""
                if duuid != UUID_REPORT_REF:
                    continue
                try:
                    d_intro = await bus.introspect("org.bluez", dpath)
                    d_proxy = bus.get_proxy_object("org.bluez", dpath, d_intro)
                    d_iface = d_proxy.get_interface("org.bluez.GattDescriptor1")
                    ref = bytes(await d_iface.call_read_value({}))
                    if ref == REPORT_REF_CONSUMER:
                        self._consumer_path = path
                    elif ref == REPORT_REF_KEYBOARD:
                        self._kbd_path = path
                    elif ref == REPORT_REF_VOICE_ADPCM:
                        self._voice_path = path
                except Exception as err:
                    _LOGGER.debug("Read ReportRef %s failed: %s", dpath, err)

        if not self._voice_path:
            _LOGGER.debug("HID Voice characteristic not ready yet for %s", self.mac_address)
            return False

        # Read initial battery and start notifications on all channels
        if self._battery_path and self._on_battery:
            try:
                b_intro = await bus.introspect("org.bluez", self._battery_path)
                b_proxy = bus.get_proxy_object("org.bluez", self._battery_path, b_intro)
                b_iface = b_proxy.get_interface("org.bluez.GattCharacteristic1")
                raw_bat = bytes(await b_iface.call_read_value({}))
                self._process_battery(raw_bat)
                await b_iface.call_start_notify()
            except Exception as err:
                if "Already notifying" not in str(err):
                    _LOGGER.debug("Battery notify error for %s: %s", self.mac_address, err)

        for label, cpath in [
            ("Consumer", self._consumer_path),
            ("Keyboard", self._kbd_path),
            ("VoiceADPCM", self._voice_path),
        ]:
            if not cpath:
                continue
            try:
                c_intro = await bus.introspect("org.bluez", cpath)
                c_proxy = bus.get_proxy_object("org.bluez", cpath, c_intro)
                c_iface = c_proxy.get_interface("org.bluez.GattCharacteristic1")
                await c_iface.call_start_notify()
            except Exception as err:
                if "Already notifying" not in str(err):
                    _LOGGER.debug("StartNotify %s failed: %s", label, err)

        _LOGGER.info("WiiM Voice Remote %s connected and ready (BlueZ)", self.mac_address)
        return True

    async def _run_bluez_dbus(self) -> None:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            # Add D-Bus match rule for PropertiesChanged from org.bluez
            reply = await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[
                        "type='signal',sender='org.bluez',"
                        "interface='org.freedesktop.DBus.Properties',"
                        "member='PropertiesChanged'"
                    ],
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(f"AddMatch failed: {reply.body}")

            resubscribe_event = asyncio.Event()
            resubscribe_event.set()

            def _on_message(msg: Message) -> None:
                if (
                    msg.message_type != MessageType.SIGNAL
                    or msg.member != "PropertiesChanged"
                    or not msg.path
                    or len(msg.body) < 2
                ):
                    return
                iface_name = msg.body[0]
                changed = msg.body[1]

                # Device connection / service resolution state changes
                if iface_name == "org.bluez.Device1" and msg.path.endswith(self._mac_suffix):
                    if "Connected" in changed:
                        is_conn = bool(changed["Connected"].value)
                        _LOGGER.debug("WiiM Voice Remote %s Connected=%s", self.mac_address, is_conn)
                        if not is_conn and self._voice_active:
                            self._voice_active = False
                            self._on_voice_stop()
                        if is_conn:
                            resubscribe_event.set()
                    if "ServicesResolved" in changed and bool(changed["ServicesResolved"].value):
                        resubscribe_event.set()
                    return

                # Live GATT notifications
                if iface_name == "org.bluez.GattCharacteristic1" and "Value" in changed:
                    raw = bytes(changed["Value"].value)
                    if msg.path == self._consumer_path:
                        self._process_consumer(raw)
                    elif msg.path == self._kbd_path:
                        self._process_keyboard(raw)
                    elif msg.path == self._voice_path:
                        self._process_voice(raw)
                    elif msg.path == self._battery_path:
                        self._process_battery(raw)

            bus.add_message_handler(_on_message)

            while not self._stop_event.is_set():
                if resubscribe_event.is_set() or self._wake_event.is_set():
                    resubscribe_event.clear()
                    self._wake_event.clear()
                    try:
                        await self._bluez_subscribe_all(bus)
                    except Exception as err:
                        _LOGGER.debug("BlueZ subscribe attempt error: %s", err)

                # Wait for either a BlueZ signal, BLE advertisement, stop, or 10s heartbeat
                wait_tasks = [
                    asyncio.create_task(resubscribe_event.wait()),
                    asyncio.create_task(self._wake_event.wait()),
                    asyncio.create_task(self._stop_event.wait()),
                ]
                done, pending = await asyncio.wait(
                    wait_tasks, timeout=10.0, return_when=asyncio.FIRST_COMPLETED
                )
                for p in pending:
                    p.cancel()
        finally:
            bus.disconnect()

    # ---- Bleak fallback transport ----

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("WiiM Voice Remote %s disconnected (Bleak)", self.mac_address)
        if self._voice_active:
            self._voice_active = False
            self._on_voice_stop()
        self._disconnected_event.set()

    def _on_consumer_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._process_consumer(bytes(data))

    def _on_keyboard_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._process_keyboard(bytes(data))

    def _on_voice_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._process_voice(bytes(data))

    def _on_battery_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        self._process_battery(bytes(data))

    async def _connect_and_subscribe_bleak(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )
        if ble_device is None:
            return

        self._disconnected_event.clear()
        client = await establish_connection(
            BleakClient,
            ble_device,
            name=f"WiiM Voice Remote ({self.mac_address})",
            disconnected_callback=self._handle_disconnect,
            max_attempts=1,
        )
        self._client = client

        try:
            try:
                await client.pair()
            except Exception:
                pass

            consumer_char = None
            kbd_char = None
            voice_char = None
            battery_char = None

            for service in client.services:
                for char in service.characteristics:
                    cuuid = char.uuid.lower()
                    if cuuid == UUID_BATTERY_LEVEL:
                        battery_char = char
                        continue
                    if cuuid != UUID_HID_REPORT:
                        continue
                    for desc in char.descriptors:
                        if desc.uuid.lower() == UUID_REPORT_REF:
                            ref = bytes(await client.read_gatt_descriptor(desc.handle))
                            if ref == REPORT_REF_CONSUMER:
                                consumer_char = char
                            elif ref == REPORT_REF_KEYBOARD:
                                kbd_char = char
                            elif ref == REPORT_REF_VOICE_ADPCM:
                                voice_char = char

            if battery_char and self._on_battery:
                try:
                    raw_bat = await client.read_gatt_char(battery_char)
                    self._process_battery(bytes(raw_bat))
                    await client.start_notify(battery_char, self._on_battery_notify)
                except Exception:
                    pass

            if not voice_char:
                return

            if consumer_char:
                await client.start_notify(consumer_char, self._on_consumer_notify)
            if kbd_char:
                await client.start_notify(kbd_char, self._on_keyboard_notify)
            await client.start_notify(voice_char, self._on_voice_notify)

            _LOGGER.info("WiiM Voice Remote %s connected and ready (Bleak)", self.mac_address)
            await self._disconnected_event.wait()
        finally:
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self._client = None

    async def _run_bleak_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                await self._connect_and_subscribe_bleak()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Bleak connection error for %s: %s", self.mac_address, err)

            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
