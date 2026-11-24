"""Battery level sensor for the WiiM Voice Remote."""

from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAC_ADDRESS, CONF_NAME, DEFAULT_NAME, DOMAIN, SIGNAL_BATTERY


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WiiM Voice Remote battery sensor."""
    mac = entry.data[CONF_MAC_ADDRESS].upper()
    name = entry.data.get(CONF_NAME, DEFAULT_NAME)
    async_add_entities([WiiMBatterySensor(mac, name)])


class WiiMBatterySensor(RestoreSensor):
    """Diagnostic battery percentage sensor for the WiiM Voice Remote."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, mac: str, device_name: str) -> None:
        self._mac = mac.upper()
        self._attr_unique_id = f"wiim_voice_remote_{self._mac.replace(':', '').lower()}_battery"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            name=device_name,
            manufacturer="WiiM / LinkPlay",
            model="WiiM Voice Remote",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None:
            if last_data.native_value is not None:
                self._attr_native_value = last_data.native_value

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_BATTERY.format(mac=self._mac),
                self._handle_battery_update,
            )
        )

    @callback
    def _handle_battery_update(self, level: int) -> None:
        self._attr_native_value = level
        self.async_write_ha_state()
