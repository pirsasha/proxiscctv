"""Text and diagnostic sensors.

The headline entity is ``sensor.<camera>_last_plate``. On Dahua cameras a plate
sensor like this is fed by the camera itself; on this platform the camera only
sends a plate over its WebSocket when *its own* recogniser reads one, which is
rare in practice (small plates, night IR). The sensor therefore reports what the
camera actually said and stays ``unknown`` otherwise — it never invents a value.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_SENSORS, MANUFACTURER
from .coordinator import PxIpcCoordinator

DIAGNOSTIC_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
    ),
    SensorEntityDescription(
        key="model",
        translation_key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cctv",
    ),
    SensorEntityDescription(
        key="serial",
        translation_key="serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:identifier",
    ),
    SensorEntityDescription(
        key="platform",
        translation_key="platform",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:memory",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        PxIpcLastPlateSensor(coordinator, entry),
        PxIpcLastEventSensor(coordinator, entry),
        PxIpcEventStreamSensor(coordinator, entry),
    ]
    entities.extend(
        PxIpcDiagnosticSensor(coordinator, entry, description)
        for description in DIAGNOSTIC_SENSORS
    )
    async_add_entities(entities)


class PxIpcBaseSensor(CoordinatorEntity[PxIpcCoordinator], SensorEntity):
    """Shared device plumbing."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

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


class PxIpcLastPlateSensor(PxIpcBaseSensor):
    """The most recent licence plate the camera reported."""

    _attr_translation_key = "last_plate"
    _attr_icon = "mdi:car-license-plate"

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_last_plate"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_event_listener(self._handle_event)
        )

    @property
    def native_value(self) -> str | None:
        return self.coordinator.last_plate

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if self.coordinator.last_plate_time:
            attributes["last_plate_time"] = dt_util.utc_from_timestamp(
                self.coordinator.last_plate_time
            ).isoformat()
        return attributes

    @callback
    def _handle_event(self, name: str, _payload: dict[str, Any]) -> None:
        if name == "license_plate":
            self.async_write_ha_state()


class PxIpcLastEventSensor(PxIpcBaseSensor):
    """The most recent event of any kind."""

    _attr_translation_key = "last_event"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_last_event"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_event_listener(self._handle_event)
        )

    @property
    def native_value(self) -> str | None:
        name = self.coordinator.last_event
        if name is None:
            return None
        return EVENT_SENSORS.get(name, name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {"event_key": self.coordinator.last_event}
        if self.coordinator.last_event_time:
            attributes["last_event_time"] = dt_util.utc_from_timestamp(
                self.coordinator.last_event_time
            ).isoformat()
        return attributes

    @callback
    def _handle_event(self, _name: str, _payload: dict[str, Any]) -> None:
        self.async_write_ha_state()


class PxIpcEventStreamSensor(PxIpcBaseSensor):
    """Whether the real-time event channel is currently up.

    Nothing is pushed while the scene is quiet, so this reports the *socket*
    state, not the freshness of events — otherwise it would look broken on a
    still night.
    """

    _attr_translation_key = "event_stream"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "disconnected"]
    _attr_icon = "mdi:access-point-network"

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_event_stream"

    @property
    def native_value(self) -> str:
        return "connected" if self.coordinator.stream_connected else "disconnected"


class PxIpcDiagnosticSensor(PxIpcBaseSensor):
    """Static-ish device facts."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_{description.key}"
        )

    @property
    def native_value(self) -> str | None:
        info = self.coordinator.data.device_info if self.coordinator.data else {}
        mapping = {
            "firmware": "firmwareVersion",
            "model": "devName",
            "serial": "devId",
            "platform": "platform",
        }
        key = mapping.get(self.entity_description.key)
        value = info.get(key) if key else None
        return str(value) if value not in (None, "") else None
