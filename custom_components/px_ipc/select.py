"""Selectable camera settings.

Only settings that were confirmed writable on the device are exposed, and each
one is written with read-modify-write of the whole parameter block: the firmware
replaces the object it is given, so sending a lone field would wipe its
neighbours.

WDR is the awkward one. ``/api/image/image-param`` accepts ``wideDynamicLevel``,
answers ``code: 0`` and changes nothing; only the legacy ``/api/image/image``
endpoint with ``enableWideDynamic`` actually applies it. That is handled inside
the client so the entity can stay simple.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PxIpcError
from .const import (
    ANTI_FLICKER_LEVELS,
    ANTI_FLICKER_LEVELS_REVERSE,
    DAY_NIGHT_MODES,
    DAY_NIGHT_MODES_REVERSE,
    DNR_LEVELS,
    DNR_LEVELS_REVERSE,
    DOMAIN,
    ILLUMINATOR_MODES,
    ILLUMINATOR_MODES_REVERSE,
    MANUFACTURER,
    MIRROR_MODES,
    MIRROR_MODES_REVERSE,
    ROTATE_ANGLES,
    ROTATE_ANGLES_REVERSE,
    WDR_LEVELS,
    WDR_LEVELS_REVERSE,
)
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    async_add_entities(
        [
            PxIpcDayNightSelect(coordinator, entry),
            PxIpcIlluminatorSelect(coordinator, entry),
            PxIpcWdrSelect(coordinator, entry),
            PxIpcAntiFlickerSelect(coordinator, entry),
            PxIpcDnrSelect(coordinator, entry),
            PxIpcMirrorSelect(coordinator, entry),
            PxIpcRotationSelect(coordinator, entry),
        ]
    )


class PxIpcBaseSelect(CoordinatorEntity[PxIpcCoordinator], SelectEntity):
    """Shared plumbing for the three selects."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        description: SelectEntityDescription,
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

    async def _apply(self, coro) -> None:
        try:
            await coro
        except PxIpcError as err:
            _LOGGER.error("could not apply %s: %s", self.entity_description.key, err)
            return
        await self.coordinator.async_request_refresh()


class PxIpcDayNightSelect(PxIpcBaseSelect):
    """auto / color / black & white / schedule."""

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="day_night_mode",
                translation_key="day_night_mode",
                icon="mdi:theme-light-dark",
                options=list(DAY_NIGHT_MODES.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.day_night if self.coordinator.data else None
        return DAY_NIGHT_MODES.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        mode = DAY_NIGHT_MODES_REVERSE.get(option)
        if mode is None:
            return
        await self._apply(self.coordinator.client.async_set_day_night(mode))


class PxIpcIlluminatorSelect(PxIpcBaseSelect):
    """warm light / infrared / intelligent."""

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="illuminator",
                translation_key="illuminator",
                icon="mdi:flashlight",
                options=list(ILLUMINATOR_MODES.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.illuminator if self.coordinator.data else None
        return ILLUMINATOR_MODES.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        mode = ILLUMINATOR_MODES_REVERSE.get(option)
        if mode is None:
            return
        await self._apply(self.coordinator.client.async_set_illuminator(mode))


class PxIpcWdrSelect(PxIpcBaseSelect):
    """off / low / medium / high.

    Needs a camera with ``supportWdr``; the firmware silently drops the write
    otherwise, so the entity simply stops changing.
    """

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="wdr",
                translation_key="wdr",
                icon="mdi:contrast-box",
                options=list(WDR_LEVELS.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.wdr if self.coordinator.data else None
        return WDR_LEVELS.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        level = WDR_LEVELS_REVERSE.get(option)
        if level is None:
            return
        await self._apply(self.coordinator.client.async_set_wdr(level))


class PxIpcAntiFlickerSelect(PxIpcBaseSelect):
    """Off, or one of the ten exposure bands the device exposes.

    Relevant to plates because anti-flicker caps the exposure time; on a lit
    street that is often the difference between a sharp plate and a smeared one.
    """

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="anti_flicker",
                translation_key="anti_flicker",
                icon="mdi:sine-wave",
                options=list(ANTI_FLICKER_LEVELS.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.anti_flicker if self.coordinator.data else None
        return ANTI_FLICKER_LEVELS.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        level = ANTI_FLICKER_LEVELS_REVERSE.get(option)
        if level is None:
            return
        await self._apply(self.coordinator.client.async_set_anti_flicker(level))


class PxIpcDnrSelect(PxIpcBaseSelect):
    """Digital noise reduction."""

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="dnr",
                translation_key="dnr",
                icon="mdi:image-filter-tilt-shift",
                options=list(DNR_LEVELS.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.dnr if self.coordinator.data else None
        return DNR_LEVELS.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        level = DNR_LEVELS_REVERSE.get(option)
        if level is None:
            return
        await self._apply(self.coordinator.client.async_set_dnr(level))


class PxIpcMirrorSelect(PxIpcBaseSelect):
    """Flip the picture when the camera is mounted upside down or sideways."""

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="mirror",
                translation_key="mirror",
                icon="mdi:flip-horizontal",
                options=list(MIRROR_MODES.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.mirror if self.coordinator.data else None
        return MIRROR_MODES.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        mode = MIRROR_MODES_REVERSE.get(option)
        if mode is None:
            return
        await self._apply(self.coordinator.client.async_set_mirror(mode))


class PxIpcRotationSelect(PxIpcBaseSelect):
    """Rotate the picture. The device offers upright, 90 and 270."""

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            SelectEntityDescription(
                key="rotation",
                translation_key="rotation",
                icon="mdi:rotate-right",
                options=list(ROTATE_ANGLES.values()),
            ),
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.data.rotation if self.coordinator.data else None
        return ROTATE_ANGLES.get(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        angle = ROTATE_ANGLES_REVERSE.get(option)
        if angle is None:
            return
        await self._apply(self.coordinator.client.async_set_rotation(angle))
