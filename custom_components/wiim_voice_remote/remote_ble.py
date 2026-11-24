"""BLE client for the WiiM Voice Remote using Home Assistant Bluetooth / Bleak."""

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
    """Maintains a BLE connection to a paired WiiM Voice Remote and dispatches voice/button callbacks."""

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
        self._on_voice_start = on_voice_start
        self._on_voice_pcm = on_voice_pcm
        self._on_voice_stop = on_voice_stop
        self._on_button = on_button
        self._on_battery = on_battery

        self._client: BleakClient | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._voice_active = False

    def start(self) -> None:
        """Start background connection loop."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = self.hass.async_create_background_task(
                self._run_loop(),
                name=f"wiim_voice_remote_{self.mac_address}",
            )

    async def async_stop(self) -> None:
        """Stop background connection loop and disconnect."""
        self._stop_event.set()
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

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("WiiM Voice Remote %s disconnected", self.mac_address)
        if self._voice_active:
            self._voice_active = False
            self._on_voice_stop()
        self._disconnected_event.set()

    def _on_consumer_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        raw = bytes(data)
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

    def _on_keyboard_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if len(data) < 3 or data[2] == 0:
            return
        preset = KBD_PRESETS.get(data[2])
        if preset is not None:
            self._on_button(f"preset_{preset}")
        else:
            _LOGGER.debug("Unmapped keyboard key from %s: 0x%02x", self.mac_address, data[2])

    def _on_voice_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if not self._voice_active:
            self._voice_active = True
            self._on_voice_start()
        pcm = decode_wiim_adpcm_packet(data)
        if pcm:
            self._on_voice_pcm(pcm)

    def _on_battery_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if data and self._on_battery:
            level = max(0, min(100, int(data[0])))
            self._on_battery(level)

    async def _connect_and_subscribe(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )
        if ble_device is None:
            _LOGGER.debug(
                "WiiM Voice Remote %s not currently visible to HA Bluetooth; waiting...",
                self.mac_address,
            )
            return

        # Ensure BLE HID bonding via BlueZ NoInputNoOutput agent if running on local BlueZ
        await async_bluez_pair_and_trust(self.mac_address)

        self._disconnected_event.clear()
        client = await establish_connection(
            BleakClient,
            ble_device,
            name=f"WiiM Voice Remote ({self.mac_address})",
            disconnected_callback=self._handle_disconnect,
            max_attempts=2,
        )
        self._client = client

        try:
            try:
                await client.pair()
                _LOGGER.debug("BLE pair() succeeded for %s", self.mac_address)
            except NotImplementedError:
                pass
            except Exception as pair_err:
                _LOGGER.debug("BLE pair() note for %s: %s", self.mac_address, pair_err)

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
                    if raw_bat:
                        self._on_battery(max(0, min(100, int(raw_bat[0]))))
                    await client.start_notify(battery_char, self._on_battery_notify)
                except Exception as bat_err:
                    _LOGGER.debug("Battery read/notify error for %s: %s", self.mac_address, bat_err)

            if not voice_char:
                _LOGGER.warning(
                    "WiiM Voice Remote %s connected, but HID Voice Report (03 01) was not found",
                    self.mac_address,
                )
                return

            if consumer_char:
                await client.start_notify(consumer_char, self._on_consumer_notify)
            if kbd_char:
                await client.start_notify(kbd_char, self._on_keyboard_notify)
            await client.start_notify(voice_char, self._on_voice_notify)

            _LOGGER.info("WiiM Voice Remote %s connected and ready", self.mac_address)
            await self._disconnected_event.wait()
        finally:
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self._client = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._connect_and_subscribe()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("BLE connection error for %s: %s", self.mac_address, err)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
