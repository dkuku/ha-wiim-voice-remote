"""Select entity for choosing the Home Assistant Assist pipeline."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import AssistPipelineSelect
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAC_ADDRESS, CONF_NAME, DEFAULT_NAME, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Assist pipeline selector for the WiiM Voice Remote."""
    mac = entry.data[CONF_MAC_ADDRESS].upper()
    name = entry.data.get(CONF_NAME, DEFAULT_NAME)
    async_add_entities([WiiMPipelineSelect(hass, mac, name)])


class WiiMPipelineSelect(AssistPipelineSelect):
    """Select which Assist pipeline (Assistant) the WiiM Voice Remote uses."""

    def __init__(self, hass: HomeAssistant, mac: str, device_name: str) -> None:
        unique_prefix = f"wiim_voice_remote_{mac.replace(':', '').lower()}"
        super().__init__(hass, DOMAIN, unique_prefix)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={(CONNECTION_BLUETOOTH, mac)},
            name=device_name,
            manufacturer="WiiM / LinkPlay",
            model="WiiM Voice Remote",
        )
