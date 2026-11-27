"""Config flow for WiiM Voice Remote with Bluetooth auto-discovery and pairing."""

from __future__ import annotations

import logging
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import establish_connection
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .bluez_pair import BlueZPairingSession
from .const import (
    CONF_MAC_ADDRESS,
    CONF_MEDIA_PLAYER,
    CONF_MUTE_ON_VOICE,
    CONF_NAME,
    CONF_WIIM_IP,
    DEFAULT_MUTE_ON_VOICE,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_mac(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


class WiiMVoiceRemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WiiM Voice Remote."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_mac: str | None = None
        self._discovered_name: str | None = None

    async def _try_pair(self, mac: str) -> None:
        """Best-effort BLE pairing during setup with active BlueZ NoInputNoOutput agent."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, mac, connectable=True
        )
        if ble_device is None:
            return
        client: BleakClient | None = None
        try:
            async with BlueZPairingSession(mac) as bluez_session:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    name=f"WiiM Voice Remote ({mac})",
                    max_attempts=2,
                )
                if await bluez_session.async_pair_and_trust(client):
                    _LOGGER.info("Paired WiiM Voice Remote %s during setup", mac)
                    return
                try:
                    await client.pair()
                    _LOGGER.info("Paired WiiM Voice Remote %s during setup", mac)
                except NotImplementedError:
                    pass
                except Exception as err:
                    _LOGGER.debug("Pair during setup note for %s: %s", mac, err)
        except Exception as err:
            _LOGGER.debug("Could not pre-pair %s during setup: %s", mac, err)
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle automatic Bluetooth discovery when remote is in pairing mode."""
        mac = _normalize_mac(discovery_info.address)
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()

        self._discovered_mac = mac
        self._discovered_name = discovery_info.name or DEFAULT_NAME
        self.context["title_placeholders"] = {
            "name": f"{self._discovered_name} ({mac})"
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovered WiiM Voice Remote and pair it."""
        assert self._discovered_mac is not None
        if user_input is not None:
            await self._try_pair(self._discovered_mac)
            data = {
                CONF_MAC_ADDRESS: self._discovered_mac,
                CONF_NAME: user_input.get(CONF_NAME) or self._discovered_name or DEFAULT_NAME,
                CONF_MEDIA_PLAYER: user_input.get(CONF_MEDIA_PLAYER) or None,
                CONF_WIIM_IP: (user_input.get(CONF_WIIM_IP) or "").strip() or None,
                CONF_MUTE_ON_VOICE: bool(
                    user_input.get(CONF_MUTE_ON_VOICE, DEFAULT_MUTE_ON_VOICE)
                ),
            }
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=self._discovered_name or DEFAULT_NAME): str,
                vol.Optional(CONF_MEDIA_PLAYER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Optional(CONF_WIIM_IP, default=""): str,
                vol.Optional(CONF_MUTE_ON_VOICE, default=DEFAULT_MUTE_ON_VOICE): bool,
            }
        )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=schema,
            description_placeholders={
                "name": self._discovered_name or DEFAULT_NAME,
                "mac": self._discovered_mac,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual or discovered selection from the Add Integration dialog."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mac = _normalize_mac(user_input[CONF_MAC_ADDRESS])
            if len(mac) != 17 or mac.count(":") != 5:
                errors[CONF_MAC_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                await self._try_pair(mac)
                data = {
                    CONF_MAC_ADDRESS: mac,
                    CONF_NAME: user_input.get(CONF_NAME) or DEFAULT_NAME,
                    CONF_MEDIA_PLAYER: user_input.get(CONF_MEDIA_PLAYER) or None,
                    CONF_WIIM_IP: (user_input.get(CONF_WIIM_IP) or "").strip() or None,
                    CONF_MUTE_ON_VOICE: bool(
                        user_input.get(CONF_MUTE_ON_VOICE, DEFAULT_MUTE_ON_VOICE)
                    ),
                }
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        # Collect nearby connectable BLE devices (WiiM devices first)
        discovered_options: list[selector.SelectOptionDict] = []
        wiim_options: list[selector.SelectOptionDict] = []
        seen: set[str] = set()
        try:
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
                addr = _normalize_mac(info.address)
                if addr in seen:
                    continue
                seen.add(addr)
                name = info.name or addr
                opt: selector.SelectOptionDict = {
                    "value": addr,
                    "label": f"{name} ({addr})",
                }
                if "wiim" in name.lower():
                    wiim_options.append(opt)
                else:
                    discovered_options.append(opt)
        except Exception:
            pass

        all_options = wiim_options + discovered_options
        if all_options:
            mac_field = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=all_options,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            default_mac = wiim_options[0]["value"] if wiim_options else vol.UNDEFINED
        else:
            mac_field = str
            default_mac = ""

        schema = vol.Schema(
            {
                vol.Required(CONF_MAC_ADDRESS, default=default_mac): mac_field,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Optional(CONF_MEDIA_PLAYER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Optional(CONF_WIIM_IP, default=""): str,
                vol.Optional(CONF_MUTE_ON_VOICE, default=DEFAULT_MUTE_ON_VOICE): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> WiiMVoiceRemoteOptionsFlow:
        return WiiMVoiceRemoteOptionsFlow(config_entry)


class WiiMVoiceRemoteOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for WiiM Voice Remote."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_MEDIA_PLAYER: user_input.get(CONF_MEDIA_PLAYER) or None,
                    CONF_WIIM_IP: (user_input.get(CONF_WIIM_IP) or "").strip() or None,
                    CONF_MUTE_ON_VOICE: bool(
                        user_input.get(CONF_MUTE_ON_VOICE, DEFAULT_MUTE_ON_VOICE)
                    ),
                },
            )

        current_mp = self._config_entry.options.get(
            CONF_MEDIA_PLAYER, self._config_entry.data.get(CONF_MEDIA_PLAYER)
        )
        current_ip = (
            self._config_entry.options.get(
                CONF_WIIM_IP, self._config_entry.data.get(CONF_WIIM_IP)
            )
            or ""
        )
        current_mute = bool(
            self._config_entry.options.get(
                CONF_MUTE_ON_VOICE,
                self._config_entry.data.get(CONF_MUTE_ON_VOICE, DEFAULT_MUTE_ON_VOICE),
            )
        )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MEDIA_PLAYER, description={"suggested_value": current_mp}
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Optional(CONF_WIIM_IP, default=current_ip): str,
                vol.Optional(CONF_MUTE_ON_VOICE, default=current_mute): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
