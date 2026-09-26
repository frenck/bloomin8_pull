from __future__ import annotations

import asyncio
import os
import time

import voluptuous as vol

from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.json import load_json

from .const import (
    CONF_DEVICE_NAME,
    CONF_DEVICES,
    CONF_IMAGE_DIR,
    CONF_ORIENTATION,
    CONF_PUBLISH_DIR,
    CONF_PUBLISH_WEBPATH,
    CONF_WAKE_UP_HOURS,
    DEFAULT_IMAGE_DIR,
    DEFAULT_ORIENTATION,
    DEFAULT_PUBLISH_DIR,
    DEFAULT_PUBLISH_WEBPATH,
    DEFAULT_WAKE_UP_HOURS,
    DOMAIN,
    PULL_MAP_TTL_SECONDS,
    STATE_BATTERY,
    STATE_ENABLED,
    STATE_FILE,
    STATE_LAST_IMAGE_URL,
    STATE_LAST_SEEN,
    STATE_SUCCESS,
)
from .helpers import default_device_state
from .view import Bloomin8PullView, Bloomin8SignalView


def _orientation(value: str) -> str:
    value = str(value).strip().upper()
    if value not in ("P", "L"):
        raise vol.Invalid("orientation must be P (portrait) or L (landscape)")
    return value


DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_NAME): cv.string,
        vol.Optional(CONF_ORIENTATION, default=DEFAULT_ORIENTATION): _orientation,
        vol.Optional(CONF_WAKE_UP_HOURS, default=DEFAULT_WAKE_UP_HOURS): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_ACCESS_TOKEN): cv.string,
                vol.Optional(CONF_IMAGE_DIR, default=DEFAULT_IMAGE_DIR): cv.string,
                vol.Optional(CONF_PUBLISH_DIR, default=DEFAULT_PUBLISH_DIR): cv.string,
                vol.Optional(CONF_PUBLISH_WEBPATH, default=DEFAULT_PUBLISH_WEBPATH): cv.string,
                vol.Required(CONF_DEVICES): vol.All(
                    {cv.string: DEVICE_SCHEMA},
                    vol.Length(min=1),
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PLATFORMS = ["sensor", "binary_sensor", "switch"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    cfg = config.get(DOMAIN)
    if not cfg:
        return True

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["cfg"] = cfg
    hass.data[DOMAIN]["device_cfg"] = dict(cfg[CONF_DEVICES])
    hass.data[DOMAIN]["device_state"] = {
        device_id: default_device_state()
        for device_id in cfg[CONF_DEVICES]
    }
    hass.data[DOMAIN]["pulls"] = {}
    hass.data[DOMAIN]["entities"] = []
    hass.data[DOMAIN]["state_lock"] = asyncio.Lock()

    if os.path.isfile(STATE_FILE):
        try:
            saved = await hass.async_add_executor_job(load_json, STATE_FILE)

            # v0.2+ state format
            if isinstance(saved.get("devices"), dict):
                for device_id, saved_state in saved["devices"].items():
                    if device_id not in hass.data[DOMAIN]["device_state"]:
                        continue
                    state = hass.data[DOMAIN]["device_state"][device_id]
                    for key in (
                        STATE_BATTERY,
                        STATE_SUCCESS,
                        STATE_LAST_SEEN,
                        STATE_ENABLED,
                        STATE_LAST_IMAGE_URL,
                    ):
                        if key in saved_state:
                            state[key] = saved_state[key]

                cutoff = time.time() - PULL_MAP_TTL_SECONDS
                for pull_id, info in (saved.get("pulls") or {}).items():
                    if not isinstance(info, dict):
                        continue
                    device_id = info.get("device_id")
                    try:
                        created_at = float(info.get("created_at", 0))
                    except (TypeError, ValueError):
                        continue
                    if (
                        device_id in hass.data[DOMAIN]["device_cfg"]
                        and created_at >= cutoff
                    ):
                        hass.data[DOMAIN]["pulls"][pull_id] = {
                            "device_id": device_id,
                            "created_at": created_at,
                        }

            # Legacy v0.1.x state format: migrate it to the first configured device.
            else:
                first_device_id = next(iter(cfg[CONF_DEVICES]))
                state = hass.data[DOMAIN]["device_state"][first_device_id]
                for key in (
                    STATE_BATTERY,
                    STATE_SUCCESS,
                    STATE_LAST_SEEN,
                    STATE_ENABLED,
                    STATE_LAST_IMAGE_URL,
                ):
                    if key in saved:
                        state[key] = saved[key]
        except Exception:
            # A damaged/old state file must never prevent Home Assistant startup.
            pass

    hass.http.register_view(Bloomin8PullView(hass, cfg))
    hass.http.register_view(Bloomin8SignalView(hass, cfg))

    for platform in PLATFORMS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))

    return True
