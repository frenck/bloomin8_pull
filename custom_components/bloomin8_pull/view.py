from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from datetime import datetime as dt, timezone
from http import HTTPStatus
import logging
import os
import random
import re
import shutil

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_IMAGE_DIR,
    CONF_ORIENTATION,
    CONF_PUBLISH_DIR,
    CONF_PUBLISH_WEBPATH,
    CONF_WAKE_UP_HOURS,
    DEFAULT_ENABLED,
    DOMAIN,
    ORIENTATION_DIRS,
    STATE_BATTERY,
    STATE_ENABLED,
    STATE_LAST_IMAGE_URL,
    STATE_LAST_SEEN,
    STATE_SUCCESS,
)
from .helpers import (
    async_persist_state,
    consume_pull,
    push_device_update,
    register_pull,
    resolve_pull,
)

_LOGGER = logging.getLogger(__name__)
ALLOWED_EXT = (".jpg",)


def calc_recent_max(n_files: int) -> int:
    # 50% of files, minimum 5, maximum 250
    return max(5, min(250, int(round(n_files * 0.5))))


def clear_publish_dir(path: str) -> None:
    if not os.path.isdir(path):
        return

    for name in os.listdir(path):
        file_path = os.path.join(path, name)
        if os.path.isfile(file_path):
            os.remove(file_path)


def _publish_image_sync(publish_dir: str, src_path: str, dst_path: str) -> None:
    """Clear this device's publish dir and copy the chosen image."""
    os.makedirs(publish_dir, exist_ok=True)
    clear_publish_dir(publish_dir)
    shutil.copyfile(src_path, dst_path)


def _list_images_sync(image_dir: str, allowed_ext: tuple[str, ...]) -> list[str]:
    return [
        name
        for name in os.listdir(image_dir)
        if os.path.isfile(os.path.join(image_dir, name))
        and name.lower().endswith(allowed_ext)
    ]


def _safe_key(value: str) -> str:
    """Return a filesystem/Store-safe representation of a device id."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return safe or "device"


def parse_wake_up_hours(raw: str | list[int] | None) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        hours = raw
    else:
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        hours = [int(p) for p in parts]

    for hour in hours:
        if hour < 0 or hour > 23:
            raise ValueError(f"wake_up_hours hour out of range: {hour}")

    return sorted(set(hours))


def next_wake_time_local(hours: list[int], now_local=None):
    if now_local is None:
        now_local = dt_util.now()

    today = now_local.date()
    candidates = [
        dt_util.as_local(datetime(today.year, today.month, today.day, hour, 0, 0))
        for hour in hours
    ]
    candidates = sorted(candidates)

    for candidate in candidates:
        if candidate > now_local:
            return candidate

    tomorrow = today + timedelta(days=1)
    return dt_util.as_local(
        datetime(tomorrow.year, tomorrow.month, tomorrow.day, hours[0], 0, 0)
    )


def next_wake_time_local_windowed(
    hours: list[int],
    now_local: datetime | None = None,
    drift_window: timedelta = timedelta(minutes=30),
) -> datetime:
    """Return next scheduled local time, skipping a slot that is too close."""
    if not hours:
        raise ValueError("No wake_up_hours configured")

    if now_local is None:
        now_local = dt_util.now()

    first = next_wake_time_local(hours, now_local=now_local)
    delta = first - now_local
    if timedelta(0) < delta <= drift_window:
        return next_wake_time_local(hours, now_local=first + timedelta(seconds=1))

    return first


async def choose_varied(hass, files: list[str], store_key: str) -> str:
    recent_max = calc_recent_max(len(files))
    store = Store(hass, 1, f"bloomin8_pull_{store_key}")

    data = await store.async_load() or {}
    recent = deque(data.get("recent", []), maxlen=recent_max)

    files_set = set(files)
    recent = deque([f for f in recent if f in files_set], maxlen=recent_max)

    recent_set = set(recent)
    candidates = [f for f in files if f not in recent_set]
    chosen = random.choice(candidates or files)

    recent.append(chosen)
    await store.async_save({"recent": list(recent)})
    return chosen


def _server_timestamp() -> str:
    return str(int(dt.now(timezone.utc).timestamp()))


def json_response_with_timestamp(
    data: dict,
    *,
    status: int = HTTPStatus.OK,
    **kwargs,
) -> web.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Server-Timestamp"] = _server_timestamp()
    return web.json_response(data, status=status, headers=headers, **kwargs)


def _next_cron_iso(device_cfg: dict) -> str:
    hours = parse_wake_up_hours(device_cfg[CONF_WAKE_UP_HOURS])
    next_local = next_wake_time_local_windowed(
        hours,
        now_local=dt_util.now(),
        drift_window=timedelta(minutes=30),
    )
    return (
        dt_util.as_utc(next_local)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class Bloomin8PullView(HomeAssistantView):
    """Implements GET /eink_pull for BLOOMIN8 devices."""

    url = "/eink_pull"
    name = "api:bloomin8_pull"
    requires_auth = False  # BLOOMIN8 uses X-Access-Token; validated below.

    def __init__(self, hass, cfg: dict) -> None:
        self.hass = hass
        self.cfg = cfg

    async def get(self, request: web.Request) -> web.Response:
        token = request.headers.get("X-Access-Token", "")
        expected = self.cfg["access_token"]
        if not expected or token != expected:
            return json_response_with_timestamp(
                {"status": 401, "type": "ERROR", "message": "Unauthorized"},
                status=HTTPStatus.UNAUTHORIZED,
            )

        device_id = request.query.get("device_id")
        pull_id = request.query.get("pull_id")
        cron_time = request.query.get("cron_time")
        battery = request.query.get("battery")

        if not device_id or not pull_id:
            return json_response_with_timestamp(
                {
                    "status": 400,
                    "type": "ERROR",
                    "message": "device_id and pull_id are required",
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        device_cfg = self.cfg[CONF_DEVICES].get(device_id)
        if device_cfg is None:
            _LOGGER.warning("Rejected eink_pull from unknown device_id=%s", device_id)
            return json_response_with_timestamp(
                {
                    "status": 400,
                    "type": "ERROR",
                    "message": "Unknown device_id",
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        device_state = self.hass.data[DOMAIN]["device_state"][device_id]

        battery_val = None
        if battery is not None:
            try:
                battery_val = int(battery)
            except ValueError:
                battery_val = None
        device_state[STATE_BATTERY] = battery_val

        register_pull(self.hass, pull_id, device_id)

        _LOGGER.debug(
            "eink_pull request: device_id=%s pull_id=%s cron_time=%s battery=%s remote=%s",
            device_id,
            pull_id,
            cron_time,
            battery,
            request.remote,
        )

        try:
            next_cron_time = _next_cron_iso(device_cfg)
        except (TypeError, ValueError) as err:
            _LOGGER.error("Invalid wake_up_hours for device %s: %s", device_id, err)
            await async_persist_state(self.hass)
            push_device_update(self.hass, device_id)
            return json_response_with_timestamp(
                {
                    "status": 500,
                    "type": "ERROR",
                    "message": "Invalid wake_up_hours configuration",
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        enabled = bool(device_state.get(STATE_ENABLED, DEFAULT_ENABLED))
        image_url = device_state.get(STATE_LAST_IMAGE_URL)

        if enabled:
            orientation = device_cfg[CONF_ORIENTATION]
            orientation_dir = ORIENTATION_DIRS[orientation]
            image_dir = os.path.join(self.cfg[CONF_IMAGE_DIR], orientation_dir)

            try:
                files = await self.hass.async_add_executor_job(
                    _list_images_sync,
                    image_dir,
                    ALLOWED_EXT,
                )
            except FileNotFoundError:
                files = []

            if not files:
                await async_persist_state(self.hass)
                push_device_update(self.hass, device_id)
                return json_response_with_timestamp(
                    {
                        "status": 204,
                        "message": f"No {orientation_dir} image available",
                        "data": {"next_cron_time": next_cron_time},
                    },
                    status=HTTPStatus.OK,
                )

            chosen = await choose_varied(
                self.hass,
                files,
                store_key=f"recent_images_{_safe_key(device_id)}",
            )
            src_path = os.path.join(image_dir, chosen)

            # Keep each frame in its own publish directory. This prevents one
            # frame from deleting another frame's still-to-be-downloaded image.
            device_publish_key = _safe_key(device_id)
            device_publish_dir = os.path.join(
                self.cfg[CONF_PUBLISH_DIR],
                device_publish_key,
            )

            _, ext = os.path.splitext(chosen)
            ext = ext.lower() if ext else ".jpg"
            timestamp = dt_util.now().strftime("%Y-%m-%d-%H-%M-%S")
            published_name = f"{timestamp}_{device_publish_key}_{orientation}{ext}"
            dst_path = os.path.join(device_publish_dir, published_name)

            try:
                await self.hass.async_add_executor_job(
                    _publish_image_sync,
                    device_publish_dir,
                    src_path,
                    dst_path,
                )
            except Exception as err:
                _LOGGER.exception(
                    "Failed to publish image %s -> %s: %s",
                    src_path,
                    dst_path,
                    err,
                )
                await async_persist_state(self.hass)
                push_device_update(self.hass, device_id)
                return json_response_with_timestamp(
                    {
                        "status": 500,
                        "type": "ERROR",
                        "message": "Failed to publish image",
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

            base = f"{request.scheme}://{request.host}"
            publish_webpath = self.cfg[CONF_PUBLISH_WEBPATH].rstrip("/")
            image_url = (
                f"{base}{publish_webpath}/{device_publish_key}/{published_name}"
            )
            device_state[STATE_LAST_IMAGE_URL] = image_url

        elif image_url is None:
            # A disabled device with no previously published image has nothing to show.
            await async_persist_state(self.hass)
            push_device_update(self.hass, device_id)
            return json_response_with_timestamp(
                {
                    "status": 204,
                    "message": "Pull disabled and no previous image available",
                    "data": {"next_cron_time": next_cron_time},
                },
                status=HTTPStatus.OK,
            )

        await async_persist_state(self.hass)
        push_device_update(self.hass, device_id)

        return json_response_with_timestamp(
            {
                "status": 200,
                "type": "SHOW",
                "message": "Image retrieved successfully",
                "data": {
                    "next_cron_time": next_cron_time,
                    "image_url": image_url,
                },
            },
            status=HTTPStatus.OK,
        )


class Bloomin8SignalView(HomeAssistantView):
    """Implements GET /eink_signal for BLOOMIN8 devices."""

    url = "/eink_signal"
    name = "api:bloomin8_signal"
    requires_auth = False

    def __init__(self, hass, cfg: dict) -> None:
        self.hass = hass
        self.cfg = cfg

    async def get(self, request: web.Request) -> web.Response:
        token = request.headers.get("X-Access-Token", "")
        expected = self.cfg["access_token"]
        if not expected or token != expected:
            return json_response_with_timestamp(
                {"status": 401, "type": "ERROR", "message": "Unauthorized"},
                status=HTTPStatus.UNAUTHORIZED,
            )

        pull_id = request.query.get("pull_id")
        success = request.query.get("success")

        if not pull_id:
            return json_response_with_timestamp(
                {"status": 400, "type": "ERROR", "message": "pull_id is required"},
                status=HTTPStatus.BAD_REQUEST,
            )

        device_id = resolve_pull(self.hass, pull_id)
        if device_id is None:
            _LOGGER.warning("Received eink_signal for unknown pull_id=%s", pull_id)
            return json_response_with_timestamp(
                {
                    "status": 404,
                    "type": "ERROR",
                    "message": "Unknown or expired pull_id",
                },
                status=HTTPStatus.NOT_FOUND,
            )

        success_val = None
        if success is not None:
            success_val = str(success).strip() == "1"

        device_state = self.hass.data[DOMAIN]["device_state"][device_id]
        device_state[STATE_SUCCESS] = success_val
        device_state[STATE_LAST_SEEN] = (
            dt.now(timezone.utc).replace(microsecond=0).isoformat()
        )

        consume_pull(self.hass, pull_id)
        await async_persist_state(self.hass)
        push_device_update(self.hass, device_id)

        _LOGGER.debug(
            "eink_signal request: device_id=%s pull_id=%s success=%s remote=%s",
            device_id,
            pull_id,
            success,
            request.remote,
        )

        return json_response_with_timestamp(
            {"status": 200, "message": "Feedback recorded"},
            status=HTTPStatus.OK,
        )
