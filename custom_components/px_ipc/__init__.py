"""PX IPC camera integration (HeroSpeed / Longse platform).

Sets up one device per camera and keeps two things running:

* a coordinate poller for the slow-moving state (device info, day/night mode,
  illuminator, WDR) — these change rarely and the API is a heavy JSON exchange;
* a WebSocket listener on ``/events`` for the fast-moving state (motion,
  intrusion, illegal parking, license plates). The camera pushes a single alert
  per occurrence, so the listener drives event-driven entities rather than a
  poll interval.

The camera keeps only one session per login, and it dies on every reboot; the
client re-authenticates transparently, so a camera restart does not require a
Home Assistant restart.
"""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PxIpcAuthError, PxIpcClient, PxIpcConnectionError, PxIpcError
from .const import DOMAIN, PLATFORMS
from .coordinator import PxIpcCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one camera from a config entry."""
    session = async_get_clientsession(hass)
    client = PxIpcClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        port=entry.data.get(CONF_PORT, 80),
    )

    try:
        device_info = await client.async_test_connection()
    except PxIpcAuthError as err:
        # Wrong credentials will not fix themselves on retry, but HA has no
        # "give up" for setup, so surface it as not-ready with a clear message.
        raise ConfigEntryNotReady(f"authentication failed: {err}") from err
    except (PxIpcConnectionError, aiohttp.ClientError) as err:
        raise ConfigEntryNotReady(f"cannot reach the camera: {err}") from err
    except PxIpcError as err:
        raise ConfigEntryNotReady(f"camera answered unexpectedly: {err}") from err

    coordinator = PxIpcCoordinator(hass, client, entry, device_info)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The event stream is a background task, not a poll: start it last so the
    # entities already exist when the first packet arrives.
    await coordinator.async_start_event_stream()
    entry.async_on_unload(coordinator.async_stop_event_stream)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: PxIpcCoordinator = entry.runtime_data
    await coordinator.async_stop_event_stream()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
