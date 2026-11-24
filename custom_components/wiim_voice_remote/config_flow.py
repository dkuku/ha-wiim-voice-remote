"""Config flow for WiiM Voice Remote."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

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


def _normalize_mac(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


class WiiMVoiceRemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WiiM Voice Remote."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            mac = _normalize_mac(user_input[CONF_MAC_ADDRESS])
            if len(mac) != 17 or mac.count(":") != 5:
                errors[CONF_MAC_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
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

        default_mac = ""
        try:
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
                if info.name and "wiim" in info.name.lower():
                    default_mac = info.address.upper()
                    break
        except Exception:
            pass

        schema = vol.Schema(
            {
                vol.Required(CONF_MAC_ADDRESS, default=default_mac): str,
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
