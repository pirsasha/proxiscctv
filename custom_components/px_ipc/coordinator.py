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

    @property
    def day_night(self) -> int | None:
        value = (self.image_params.get("dayNightMode") or {}).get("dayNightMode")
        return int(value) if isinstance(value, int) else None

    @property
    def illuminator(self) -> int | None:
        value = (self.image_params.get("dayNightMode") or {}).get("ledMode")
        return int(value) if isinstance(value, int) else None

    @property
    def wdr(self) -> int | None:
        if not self.legacy_params.get("enableWideDynamic"):
            return 0
        value = self.legacy_params.get("wideDynamicLevel")
        return int(value) if isinstance(value, int) else None


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
        try:
            return PxIpcData(
                device_info=await self.client.async_get_device_info(),
                image_params=await self.client.async_get_image_params(),
                legacy_params=await self.client.async_get_legacy_image_params(),
            )
        except PxIpcError as err:
            raise UpdateFailed(str(err)) from err

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
