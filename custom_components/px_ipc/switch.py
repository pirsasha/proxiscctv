"""Switches for the camera settings that are genuinely on/off.

High light compensation is the one that matters most here: it is what stops a
retroreflective plate (and oncoming headlights) from blowing out at night, and
turning it on was measured to fix an unreadable plate on this hardware. Motion
detection is exposed because it is the noisiest source of events and the first
thing anyone wants to silence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
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


@dataclass(frozen=True, kw_only=True)
class PxIpcSwitch(SwitchEntityDescription):
    """A switch wired to one camera property and one client setter."""

    getter: str
    setter: str


SWITCHES: tuple[PxIpcSwitch, ...] = (
    PxIpcSwitch(
        key="hlc",
        translation_key="hlc",
        icon="mdi:weather-sunset-up",
        getter="hlc",
        setter="async_set_hlc",
    ),
    PxIpcSwitch(
        key="motion_detection",
        translation_key="motion_detection",
        icon="mdi:motion-sensor",
        getter="motion_enabled",
        setter="async_set_motion_enabled",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    async_add_entities(
        PxIpcSwitchEntity(coordinator, entry, description) for description in SWITCHES
    )


class PxIpcSwitchEntity(CoordinatorEntity[PxIpcCoordinator], SwitchEntity):
    """One boolean camera setting."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        description: PxIpcSwitch,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self.entity_description = description
        self._attr_unique_id = (
            f"{entry.unique_id or entry.entry_id}_{description.key}"
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
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return getattr(self.coordinator.data, self.entity_description.getter, None)

    @property
    def available(self) -> bool:
        """Hide settings this firmware does not report at all."""
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs) -> None:
        await self._apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._apply(False)

    async def _apply(self, value: bool) -> None:
        setter = getattr(self.coordinator.client, self.entity_description.setter)
        try:
            await setter(value)
        except PxIpcError as err:
            _LOGGER.error("could not set %s: %s", self.entity_description.key, err)
            return
        await self.coordinator.async_request_refresh()
