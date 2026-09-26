from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DEFAULT_ENABLED, DOMAIN, STATE_ENABLED
from .helpers import async_persist_state, push_device_update


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    entities = [
        Bloomin8PullEnabledSwitch(hass, device_id, device_cfg)
        for device_id, device_cfg in hass.data[DOMAIN]["device_cfg"].items()
    ]
    async_add_entities(entities, True)


class Bloomin8PullEnabledSwitch(SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sync"

    def __init__(self, hass, device_id: str, device_cfg: dict):
        self.hass = hass
        self.device_id = device_id
        self._device_name = device_cfg["name"]
        self._attr_name = f"BLOOMIN8 Pull {self._device_name} Pull Enabled"
        self._attr_unique_id = f"{device_id}_pull_enabled"

    @property
    def is_on(self) -> bool:
        state = self.hass.data[DOMAIN]["device_state"][self.device_id]
        return bool(state.get(STATE_ENABLED, DEFAULT_ENABLED))

    async def async_turn_on(self, **kwargs):
        self.hass.data[DOMAIN]["device_state"][self.device_id][STATE_ENABLED] = True
        await async_persist_state(self.hass)
        push_device_update(self.hass, self.device_id)

    async def async_turn_off(self, **kwargs):
        self.hass.data[DOMAIN]["device_state"][self.device_id][STATE_ENABLED] = False
        await async_persist_state(self.hass)
        push_device_update(self.hass, self.device_id)

    async def async_added_to_hass(self):
        self.hass.data[DOMAIN]["entities"].append(self)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self in self.hass.data[DOMAIN].get("entities", []):
            self.hass.data[DOMAIN]["entities"].remove(self)
