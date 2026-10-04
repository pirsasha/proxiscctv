"""Numeric camera settings.

Only settings that were confirmed writable on the device are exposed. Every one
of these was verified by writing a different value and reading it back, because
this firmware answers ``code: 0`` to writes it silently discards — an entity for
such a setting looks fine and does nothing.

The verification result is why the image tuning lives here rather than in a
"modern" endpoint: ``/api/image/image-param`` accepts brightness, contrast and
saturation, reports success and keeps the old value. Only the legacy
``/api/image/image`` endpoint actually applies them, so the setters for those
three go through it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PxIpcError
from .const import BITRATE_BOUNDS, DOMAIN, MANUFACTURER
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)

#: A literal rather than ``UnitOfRatio.PERCENTAGE``: that enum member was renamed
#: across Home Assistant releases, and a plain string is accepted everywhere.
PERCENT = "%"


@dataclass(frozen=True, kw_only=True)
class PxIpcNumber(NumberEntityDescription):
    """A number entity wired to one camera property and one client setter."""

    #: Property on :class:`~.coordinator.PxIpcData` holding the current value.
    getter: str
    #: Method on :class:`~.api.PxIpcClient` that writes it.
    setter: str
    min_value: float
    max_value: float
    unit: str | None = None


NUMBERS: tuple[PxIpcNumber, ...] = (
    PxIpcNumber(
        key="light_brightness",
        translation_key="light_brightness",
        icon="mdi:brightness-6",
        getter="light_brightness",
        setter="async_set_light_brightness",
        min_value=0,
        max_value=100,
        unit=PERCENT,
    ),
    PxIpcNumber(
        key="brightness",
        translation_key="brightness",
        icon="mdi:brightness-5",
        getter="brightness",
        setter="async_set_legacy_brightness",
        min_value=0,
        max_value=255,
    ),
    PxIpcNumber(
        key="contrast",
        translation_key="contrast",
        icon="mdi:contrast-circle",
        getter="contrast",
        setter="async_set_legacy_contrast",
        min_value=0,
        max_value=255,
    ),
    PxIpcNumber(
        key="saturation",
        translation_key="saturation",
        icon="mdi:water-opacity",
        getter="saturation",
        setter="async_set_legacy_saturation",
        min_value=0,
        max_value=255,
    ),
    PxIpcNumber(
        key="hlc_strength",
        translation_key="hlc_strength",
        icon="mdi:weather-sunny-alert",
        getter="hlc_strength",
        setter="async_set_hlc_strength",
        min_value=0,
        max_value=255,
    ),
    PxIpcNumber(
        key="shutter",
        translation_key="shutter",
        icon="mdi:camera-iris",
        getter="shutter",
        setter="async_set_shutter",
        min_value=0,
        # The device advertises 0..18 on this firmware; a higher index is a
        # shorter exposure, which is what freezes a moving car at night.
        max_value=18,
    ),
    PxIpcNumber(
        key="motion_sensitivity",
        translation_key="motion_sensitivity",
        icon="mdi:run-fast",
        getter="motion_sensitivity",
        setter="async_set_motion_sensitivity",
        min_value=1,
        max_value=100,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    entities: list[NumberEntity] = [
        PxIpcNumberEntity(coordinator, entry, description) for description in NUMBERS
    ]
    # One bitrate per stream: the main stream is what the ANPR pipeline reads,
    # the sub stream is what a dashboard previews.
    entities.extend(
        PxIpcBitrateNumber(coordinator, entry, stream) for stream in ("main", "sub")
    )
    async_add_entities(entities)


class PxIpcNumberEntity(CoordinatorEntity[PxIpcCoordinator], NumberEntity):
    """One numeric camera setting."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        description: PxIpcNumber,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_{description.key}"
        )
        self._attr_native_min_value = description.min_value
        self._attr_native_max_value = description.max_value
        self._attr_native_unit_of_measurement = description.unit

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

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = getattr(self.coordinator.data, self.entity_description.getter, None)
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def available(self) -> bool:
        """Hide settings this firmware does not report at all."""
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        setter = getattr(self.coordinator.client, self.entity_description.setter)
        try:
            await setter(int(value))
        except PxIpcError as err:
            _LOGGER.error("could not set %s: %s", self.entity_description.key, err)
            return
        await self.coordinator.async_request_refresh()


class PxIpcBitrateNumber(CoordinatorEntity[PxIpcCoordinator], NumberEntity):
    """Bitrate of one encoder stream, in kbps.

    The main stream carries 4K and is what recognition reads, so its bitrate is
    the single most useful quality lever on a slow uplink. The sub stream is what
    a dashboard pulls.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 64

    def __init__(
        self, coordinator: PxIpcCoordinator, entry: ConfigEntry, stream: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._stream = stream
        self._attr_translation_key = f"bitrate_{stream}"
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_bitrate_{stream}"
        )
        self._attr_native_unit_of_measurement = "kbps"
        low, high = BITRATE_BOUNDS.get(stream, (64, 16384))
        self._attr_native_min_value = low
        self._attr_native_max_value = high

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

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.stream_bitrate(self._stream)
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_stream_bitrate(
                self._stream, int(value)
            )
        except PxIpcError as err:
            _LOGGER.error("could not set %s bitrate: %s", self._stream, err)
            return
        await self.coordinator.async_request_refresh()
