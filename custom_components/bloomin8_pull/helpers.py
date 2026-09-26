from __future__ import annotations

import json
import time
from pathlib import Path

from .const import (
    DEFAULT_ENABLED,
    DOMAIN,
    PULL_MAP_TTL_SECONDS,
    STATE_BATTERY,
    STATE_ENABLED,
    STATE_FILE,
    STATE_FILE_VERSION,
    STATE_LAST_IMAGE_URL,
    STATE_LAST_SEEN,
    STATE_SUCCESS,
)


def default_device_state() -> dict:
    return {
        STATE_BATTERY: None,
        STATE_SUCCESS: None,
        STATE_LAST_SEEN: None,
        STATE_ENABLED: DEFAULT_ENABLED,
        STATE_LAST_IMAGE_URL: None,
    }


def _write_json_sync(path: str, data: dict) -> None:
    """Write JSON atomically. Called in an executor."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def prune_pull_map(hass) -> None:
    """Drop stale or invalid pull_id mappings from memory."""
    pulls = hass.data[DOMAIN].setdefault("pulls", {})
    configured_devices = set(hass.data[DOMAIN].get("device_cfg", {}))
    cutoff = time.time() - PULL_MAP_TTL_SECONDS

    stale = []
    for pull_id, info in pulls.items():
        try:
            created_at = float(info.get("created_at", 0))
        except (TypeError, ValueError):
            created_at = 0
        device_id = info.get("device_id")
        if created_at < cutoff or device_id not in configured_devices:
            stale.append(pull_id)

    for pull_id in stale:
        pulls.pop(pull_id, None)


def register_pull(hass, pull_id: str, device_id: str) -> None:
    prune_pull_map(hass)
    hass.data[DOMAIN].setdefault("pulls", {})[pull_id] = {
        "device_id": device_id,
        "created_at": time.time(),
    }


def resolve_pull(hass, pull_id: str) -> str | None:
    prune_pull_map(hass)
    info = hass.data[DOMAIN].setdefault("pulls", {}).get(pull_id)
    if not info:
        return None
    return info.get("device_id")


def consume_pull(hass, pull_id: str) -> None:
    hass.data[DOMAIN].setdefault("pulls", {}).pop(pull_id, None)


def serialized_state(hass) -> dict:
    prune_pull_map(hass)
    return {
        "version": STATE_FILE_VERSION,
        "devices": hass.data[DOMAIN].get("device_state", {}),
        "pulls": hass.data[DOMAIN].get("pulls", {}),
    }


async def async_persist_state(hass) -> None:
    """Persist all device states and outstanding pull mappings."""
    lock = hass.data[DOMAIN]["state_lock"]
    async with lock:
        data = serialized_state(hass)
        await hass.async_add_executor_job(_write_json_sync, STATE_FILE, data)


def push_device_update(hass, device_id: str) -> None:
    """Push a state refresh to entities belonging to one BLOOMIN8 device."""
    for entity in hass.data[DOMAIN].get("entities", []):
        if getattr(entity, "device_id", None) == device_id:
            entity.async_write_ha_state()
