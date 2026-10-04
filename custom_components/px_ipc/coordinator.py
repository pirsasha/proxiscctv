"""Coordinator: slow state by polling, fast state by WebSocket.

Splitting the two matters. Polling ``/api/image/image-param`` every few seconds
would hammer a device that answers with a few kilobytes of JSON per call, while
events arrive on their own schedule over ``/events``. So:

* :meth:`PxIpcCoordinator._async_update_data` polls device info, image params and
  the legacy image params (WDR lives only there) on a slow interval;
* the WebSocket listener decodes packets, normalises the event names and hands
  them to whoever subscribed. Entities react immediately instead of waiting for
  the next poll.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PxIpcClient, PxIpcError, PxIpcEvent
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_TYPES,
    HTTP_EVENT_TYPES,
    PACKET_NAMES,
    PKT_EVENT,
    PKT_LICENSE_PLATE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PxIpcData:
    """Everything the poller refreshes."""

    device_info: dict[str, Any] = field(default_factory=dict)
    image_params: dict[str, Any] = field(default_factory=dict)
    legacy_params: dict[str, Any] = field(default_factory=dict)
    motion: dict[str, Any] = field(default_factory=dict)
    osd: dict[str, Any] = field(default_factory=dict)
    roi: dict[str, Any] = field(default_factory=dict)
    video_encode: dict[str, Any] = field(default_factory=dict)

    # -- nested lookups ------------------------------------------------- #
    def _image(self, section: str, key: str) -> Any:
        return (self.image_params.get(section) or {}).get(key)

    def _number(self, value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def day_night(self) -> int | None:
        return self._number(self._image("dayNightMode", "dayNightMode"))

    @property
    def illuminator(self) -> int | None:
        return self._number(self._image("dayNightMode", "ledMode"))

    @property
    def light_brightness(self) -> int | None:
        return self._number(self._image("dayNightMode", "lightBrightness"))

    @property
    def wdr(self) -> int | None:
        if not self.legacy_params.get("enableWideDynamic"):
            return 0
        return self._number(self.legacy_params.get("wideDynamicLevel"))

    @property
    def hlc(self) -> bool | None:
        value = self._image("backlight", "enableStrongLightInhibition")
        return bool(value) if value is not None else None

    @property
    def hlc_strength(self) -> int | None:
        return self._number(self._image("backlight", "strongLightInhibitionStrength"))

    @property
    def shutter(self) -> int | None:
        return self._number(self._image("exposure", "electronicShutte"))

    @property
    def anti_flicker(self) -> int | None:
        return self._number(self._image("exposure", "antiFlickerLevel"))

    @property
    def dnr(self) -> int | None:
        return self._number(self._image("imageEnhance", "dnrLevel"))

    @property
    def brightness(self) -> int | None:
        return self._number(self.legacy_params.get("brightness"))

    @property
    def contrast(self) -> int | None:
        return self._number(self.legacy_params.get("contrast"))

    @property
    def saturation(self) -> int | None:
        return self._number(self.legacy_params.get("saturation"))

    @property
    def motion_enabled(self) -> bool | None:
        value = self.motion.get("enable")
        return bool(value) if value is not None else None

    @property
    def motion_sensitivity(self) -> int | None:
        return self._number(self.motion.get("sensitivity"))

    @property
    def mirror(self) -> int | None:
        return self._number(self.osd.get("mirrorMode"))

    @property
    def rotation(self) -> int | None:
        return self._number(self.osd.get("rotateAngle"))

    @property
    def roi_enabled(self) -> bool | None:
        value = self.roi.get("enable")
        return bool(value) if value is not None else None

    def stream_bitrate(self, stream: str) -> int | None:
        from .const import STREAM_ENCODE_INDEX

        streams = self.video_encode.get("streamEncode") or []
        index = STREAM_ENCODE_INDEX.get(stream, 0)
        if index >= len(streams):
            return None
        return self._number(streams[index].get("bitRate"))


#: Called as ``listener(event_name, payload)`` for every decoded event.
EventCallback = Callable[[str, dict[str, Any]], None]


class PxIpcCoordinator(DataUpdateCoordinator[PxIpcData]):
    """Shared state for one camera."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PxIpcClient,
        entry: ConfigEntry,
        device_info: dict[str, Any],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data.get('host')}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.entry = entry
        self.device_info = device_info

        self.last_plate: str | None = None
        self.last_plate_time: float | None = None
        self.last_event: str | None = None
        self.last_event_time: float | None = None
        #: name -> monotonic timestamp of the most recent trigger.
        self.event_times: dict[str, float] = {}

        # NOT ``self._listeners``: DataUpdateCoordinator owns that name and keeps
        # a dict there, so overwriting it makes async_update_listeners() crash
        # with "'list' object has no attribute 'values'" on the very first
        # refresh. These are our own event subscribers, kept separate.
        self._event_listeners: list[EventCallback] = []
        self._stream_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._connected = False

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> PxIpcData:
        """Refresh the three parameter blocks, tolerating a partial failure.

        An embedded camera occasionally stalls for a second or two — a
        concurrent writer (the vendor web UI, a script, another client) is
        enough. Treating that as a hard failure flips *every* entity to
        "unavailable" and then back, which reads as "the integration is flaky"
        while the device is fine. So each block is fetched independently: one
        that fails keeps its previous value, and only a total failure — which
        really does mean the camera is gone — raises.
        """
        failures: list[str] = []
        previous = self.data

        device_info = await self._safe(self.client.async_get_device_info, failures)
        image_params = await self._safe(self.client.async_get_image_params, failures)
        legacy_params = await self._safe(
            self.client.async_get_legacy_image_params, failures
        )
        motion = await self._safe(self.client.async_get_motion, failures)
        osd = await self._safe(self.client.async_get_osd, failures)
        roi = await self._safe(self.client.async_get_roi, failures)
        video_encode = await self._safe(self.client.async_get_video_encode, failures)

        if not any((device_info, image_params, legacy_params, motion, osd, roi, video_encode)):
            raise UpdateFailed("; ".join(failures) or "the camera did not answer")

        if failures:
            _LOGGER.debug("partial refresh, kept previous values: %s", failures)

        def keep(new: dict[str, Any] | None, old: dict[str, Any] | None) -> dict[str, Any]:
            if new is not None:
                return new
            return old or {}

        return PxIpcData(
            device_info=keep(device_info, previous.device_info if previous else {}),
            image_params=keep(image_params, previous.image_params if previous else {}),
            legacy_params=keep(
                legacy_params, previous.legacy_params if previous else {}
            ),
            motion=keep(motion, previous.motion if previous else {}),
            osd=keep(osd, previous.osd if previous else {}),
            roi=keep(roi, previous.roi if previous else {}),
            video_encode=keep(
                video_encode, previous.video_encode if previous else {}
            ),
        )

    @staticmethod
    async def _safe(call, failures: list[str]) -> dict[str, Any] | None:
        """Await *call*, recording the reason instead of raising."""
        try:
            return await call()
        except PxIpcError as err:
            failures.append(str(err))
            return None
        except Exception as err:  # noqa: BLE001 - one bad block must not sink the poll
            failures.append(f"{type(err).__name__}: {err}")
            return None

    # ------------------------------------------------------------------ #
    # Event stream
    # ------------------------------------------------------------------ #
    @property
    def stream_connected(self) -> bool:
        return self._connected

    async def async_start_event_stream(self) -> None:
        if self._stream_task is not None:
            return
        self._stop = asyncio.Event()
        self._stream_task = self.hass.async_create_background_task(
            self.client.async_listen_events(
                self._on_event, self._stop, on_state=self._on_stream_state
            ),
            name=f"{DOMAIN}-events",
        )

    async def async_stop_event_stream(self) -> None:
        self._stop.set()
        task, self._stream_task = self._stream_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - best effort
                pass

    async def async_shutdown(self) -> None:
        await self.async_stop_event_stream()

    @callback
    def async_add_event_listener(self, listener: EventCallback) -> Callable[[], None]:
        """Subscribe to decoded events; returns an unsubscribe callable."""
        self._event_listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

        return _remove

    @callback
    def _on_stream_state(self, connected: bool) -> None:
        self._connected = connected
        if connected:
            _LOGGER.debug("event stream connected")
        else:
            _LOGGER.debug("event stream disconnected")

    @callback
    def _on_event(self, event: PxIpcEvent) -> None:
        """Normalise one packet and fan it out.

        Event packets carry an ``active`` flag: ``False`` means the condition
        cleared, so the corresponding sensor is switched off immediately instead
        of waiting for its hold timer to run out.
        """
        names: list[str] = []

        if event.packet_type == PKT_LICENSE_PLATE or event.plates:
            for plate in event.plates:
                self.last_plate = plate
                self.last_plate_time = time.time()
                names.append("license_plate")
                _LOGGER.info("camera reported plate %s", plate)

        if event.packet_type == PKT_EVENT:
            for raw in event.event_types:
                names.append(EVENT_TYPES.get(raw, HTTP_EVENT_TYPES.get(raw, raw)))

        if not names:
            if event.packet_type not in (PKT_EVENT, PKT_LICENSE_PLATE):
                # Smart-object/object-info packets are informational; keeping
                # them out of the sensors stops them flapping.
                _LOGGER.debug("packet %s ignored", PACKET_NAMES.get(event.packet_type))
            return

        active = event.payload.get("active")
        now = time.time()

        for name in names:
            if active is False:
                # Explicit "cleared" from the camera.
                self.event_times.pop(name, None)
            else:
                self.event_times[name] = now
            self.last_event = name
            self.last_event_time = now
            for listener in list(self._event_listeners):
                try:
                    listener(name, event.payload)
                except Exception:  # pragma: no cover - a listener must not kill the stream
                    _LOGGER.exception("event listener failed for %s", name)

        self.async_update_listeners()
