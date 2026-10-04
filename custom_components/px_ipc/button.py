"""Action buttons.

Deliberately small. Two obvious candidates are *not* here:

* **Snapshot** — Home Assistant already exposes ``camera.snapshot`` for every
  camera entity, and the integration's camera serves a still image on demand, so
  a second button would just duplicate a service that already exists.
* **Reboot on a timer** — that belongs in an automation around this button, not
  in the integration.

The reboot button is marked as a diagnostic control and carries no confirmation
step: it is a normal, recoverable operation, and Home Assistant's UI already
asks before running a button an automation did not trigger.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
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

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="reboot",
        translation_key="reboot",
        icon="mdi:restart",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    async_add_entities(
        PxIpcRebootButton(coordinator, entry, description)
        for description in BUTTONS
    )


class PxIpcRebootButton(CoordinatorEntity[PxIpcCoordinator], ButtonEntity):
    """Restart the camera.

    The camera drops its session on the way down, so the next poll fails and the
    client re-authenticates by itself — no Home Assistant restart and no user
    action. That is also why pressing this does not need to tear the entry down.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PxIpcCoordinator,
        entry: ConfigEntry,
        description: ButtonEntityDescription,
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

    async def async_press(self) -> None:
        _LOGGER.warning("rebooting the camera at %s", self._entry.data["host"])
        try:
            await self.coordinator.client.async_reboot()
        except PxIpcError as err:
            # A reboot often severs the connection before a reply lands, so a
            # transport error here is the expected outcome, not a failure.
            _LOGGER.debug("reboot returned %s (expected while the camera restarts)", err)
