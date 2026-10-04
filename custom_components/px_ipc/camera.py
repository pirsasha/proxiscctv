"""Camera entity: live RTSP stream plus an API snapshot as the still image.

The still image deliberately comes from ``/api/picture/snapshot`` rather than the
stream, because that endpoint returns a full-resolution frame even when the
configured stream is the low-bandwidth sub stream. Cameras of this family also
serve a JPEG over HTTP, but the path is model-specific; the documented API call
is the portable one.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PxIpcError
from .const import CONF_STREAM, DOMAIN, MANUFACTURER, STREAM_SUB
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PxIpcCoordinator = entry.runtime_data
    async_add_entities([PxIpcCamera(coordinator, entry)])


class PxIpcCamera(CoordinatorEntity[PxIpcCoordinator], Camera):
    """The camera itself."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: PxIpcCoordinator, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_camera"

    # ------------------------------------------------------------------ #
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
    def available(self) -> bool:
        return super().available

    # ------------------------------------------------------------------ #
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return await self.coordinator.client.async_get_snapshot()
        except PxIpcError as err:
            _LOGGER.warning("snapshot failed: %s", err)
            return None
        except Exception:  # noqa: BLE001 - a camera must not break the whole entity
            _LOGGER.exception("unexpected snapshot failure")
            return None

    async def stream_source(self) -> str | None:
        """RTSP URL for the configured stream.

        ``stream`` must be re-read from the options each call so switching the
        stream takes effect without a reload.
        """
        stream = self._entry.options.get(
            CONF_STREAM, self._entry.data.get(CONF_STREAM, "main")
        )
        main_url = self.coordinator.client.rtsp_url
        if stream != STREAM_SUB:
            return main_url
        return main_url.replace("/main/", "/sub/")
