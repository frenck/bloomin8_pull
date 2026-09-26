from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN, STATE_BATTERY, STATE_LAST_SEEN


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    entities = [
        Bloomin8BatterySensor(hass, device_id, device_cfg)
        for device_id, device_cfg in hass.data[DOMAIN]["device_cfg"].items()
    ]
    async_add_entities(entities, True)


class Bloomin8BatterySensor(SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(self, hass, device_id: str, device_cfg: dict):
        self.hass = hass
        self.device_id = device_id
        self._device_name = device_cfg["name"]
        self._attr_name = f"BLOOMIN8 Pull {self._device_name} Battery"
        self._attr_unique_id = f"{device_id}_battery"

    @property
    def native_value(self):
        return self.hass.data[DOMAIN]["device_state"][self.device_id].get(STATE_BATTERY)

    @property
    def extra_state_attributes(self):
        state = self.hass.data[DOMAIN]["device_state"][self.device_id]
        return {
            "last_seen": state.get(STATE_LAST_SEEN),
            "device_id": self.device_id,
        }

    async def async_added_to_hass(self):
        self.hass.data[DOMAIN]["entities"].append(self)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self in self.hass.data[DOMAIN].get("entities", []):
            self.hass.data[DOMAIN]["entities"].remove(self)
