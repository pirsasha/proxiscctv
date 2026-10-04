"""Numeric camera settings.

Only the illuminator level is exposed. The camera reports the allowed range, but
models differ, so the range is clamped to what the device itself advertised and
falls back to 0-100 when it says nothing.
"""

from __future__ import annotations

import logging

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
from .const import DOMAIN, MANUFACTURER
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN = 0
DEFAULT_MAX = 100

#: Literal rather than ``UnitOfRatio.PERCENTAGE``: that enum member was renamed
#: across Home Assistant releases, and a plain string is accepted everywhere.
PERCENT = "%"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    async_add_entities([PxIpcLightBrightness(coordinator, entry)])


class PxIpcLightBrightness(CoordinatorEntity[PxIpcCoordinator], NumberEntity):
    """Illuminator output level."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1
    _attr_icon = "mdi:brightness-6"
    _attr_translation_key = "light_brightness"

    entity_description = NumberEntityDescription(
        key="light_brightness",
        translation_key="light_brightness",
        native_unit_of_measurement=PERCENT,
        mode=NumberMode.SLIDER,
    )

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_light_brightness"
        )

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
    def _block(self) -> dict:
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.image_params.get("dayNightMode") or {}

    @property
    def native_min_value(self) -> float:
        return float(self._block.get("lightBrightnessMin", DEFAULT_MIN))

    @property
    def native_max_value(self) -> float:
        return float(self._block.get("lightBrightnessMax", DEFAULT_MAX))

    @property
    def native_value(self) -> float | None:
        value = self._block.get("lightBrightness")
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.async_set_light_brightness(int(value))
        except PxIpcError as err:
            _LOGGER.error("could not set illuminator brightness: %s", err)
            return
        await self.coordinator.async_request_refresh()
