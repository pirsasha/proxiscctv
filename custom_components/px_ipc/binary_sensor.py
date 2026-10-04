"""Event-driven binary sensors.

The camera reports detections as they happen, not as a pollable state, so each
sensor is driven by a coordinator callback. Two things keep them usable in
automations:

* an event turns the sensor on and starts a hold timer, because the camera sends
  one alert per occurrence and nothing else;
* an event carrying ``active: false`` switches it off at once, so a condition
  that clears does not linger for the whole hold window.

Sensors are created lazily: a model that never reports, say, face detection would
otherwise leave a permanently ``unknown`` entity in the registry.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_HOLD_SECONDS, EVENT_SENSORS, MANUFACTURER
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)

#: Which HA device class fits, where one does.
DEVICE_CLASSES = {
    "motion": BinarySensorDeviceClass.MOTION,
    "video_tampering": BinarySensorDeviceClass.TAMPER,
    "video_loss": BinarySensorDeviceClass.PROBLEM,
    "audio_detection": BinarySensorDeviceClass.SOUND,
    "intrusion": BinarySensorDeviceClass.MOTION,
    "illegal_parking": BinarySensorDeviceClass.MOTION,
    "line_crossing": BinarySensorDeviceClass.MOTION,
    "license_plate": BinarySensorDeviceClass.MOTION,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data

    entities = [
        PxIpcEventSensor(coordinator, entry, name, label)
        for name, label in EVENT_SENSORS.items()
    ]
    async_add_entities(entities)


class PxIpcEventSensor(CoordinatorEntity[PxIpcCoordinator], BinarySensorEntity):
    """One camera event as a binary sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        event_name: str,
        label: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._event_name = event_name
        self._attr_translation_key = event_name
        self._attr_name = label
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_{event_name}"
        )
        self._attr_device_class = DEVICE_CLASSES.get(event_name)
        self._cancel_expiry: Any = None

    @property
    def device_info(self) -> DeviceInfo:
        info = self.coordinator.data.device_info if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._entry.unique_id or self._entry.entry_id))},
            manufacturer=str(info.get("manufacturers") or MANUFACTURER),
            model=str(info.get("platform") or "PX IPC"),
            name=str(info.get("devName") or "PX IPC"),
            sw_version=str(info.get("firmwareVersion") or ""),
            configuration_url=f"http://{self._entry.data['host']}",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_event_listener(self._handle_event)
        )
        self.async_on_remove(self._cancel_pending_expiry)

    # ------------------------------------------------------------------ #
    @property
    def is_on(self) -> bool:
        return self._event_name in self.coordinator.event_times

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {"event": self._event_name}
        triggered = self.coordinator.event_times.get(self._event_name)
        if triggered is not None:
            attributes["last_triggered"] = triggered
        if self._event_name == "license_plate" and self.coordinator.last_plate:
            attributes["plate"] = self.coordinator.last_plate
        return attributes

    # ------------------------------------------------------------------ #
    @callback
    def _handle_event(self, name: str, payload: dict[str, Any]) -> None:
        if name != self._event_name:
            return
        if self.is_on:
            self._schedule_expiry()
        else:
            self._cancel_pending_expiry()
        self.async_write_ha_state()

    @callback
    def _schedule_expiry(self) -> None:
        self._cancel_pending_expiry()
        self._cancel_expiry = async_call_later(
            self.hass, EVENT_HOLD_SECONDS, self._expire
        )

    @callback
    def _cancel_pending_expiry(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _expire(self, _now: Any) -> None:
        self._cancel_expiry = None
        # The coordinator owns the timestamps; drop ours and re-render.
        self.coordinator.event_times.pop(self._event_name, None)
        self.async_write_ha_state()
