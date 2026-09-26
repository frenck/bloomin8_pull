from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN, STATE_LAST_IMAGE_URL, STATE_LAST_SEEN, STATE_SUCCESS


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    entities = [
        Bloomin8LastSuccessBinarySensor(hass, device_id, device_cfg)
        for device_id, device_cfg in hass.data[DOMAIN]["device_cfg"].items()
    ]
    async_add_entities(entities, True)


class Bloomin8LastSuccessBinarySensor(BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:check-circle"

    def __init__(self, hass, device_id: str, device_cfg: dict):
        self.hass = hass
        self.device_id = device_id
        self._device_name = device_cfg["name"]
        self._attr_name = f"BLOOMIN8 Pull {self._device_name} Last Pull Success"
        self._attr_unique_id = f"{device_id}_last_pull_success"

    @property
    def is_on(self):
        return self.hass.data[DOMAIN]["device_state"][self.device_id].get(STATE_SUCCESS)

    @property
    def extra_state_attributes(self):
        state = self.hass.data[DOMAIN]["device_state"][self.device_id]
        return {
            "last_seen": state.get(STATE_LAST_SEEN),
            "last_image_url": state.get(STATE_LAST_IMAGE_URL),
            "device_id": self.device_id,
        }

    async def async_added_to_hass(self):
        self.hass.data[DOMAIN]["entities"].append(self)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self in self.hass.data[DOMAIN].get("entities", []):
            self.hass.data[DOMAIN]["entities"].remove(self)
