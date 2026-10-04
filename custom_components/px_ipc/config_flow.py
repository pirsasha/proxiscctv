"""Config flow for PX IPC cameras.

The camera has no discovery of any kind — no ONVIF, no mDNS, no SSDP (all
verified: ``/onvif/*`` answers 404 and the device advertises nothing) — so this
is a plain manual setup. Validation is a real login followed by a device-info
read: the API answers ``code: 0`` to almost anything, so only a genuine
authenticated call proves the credentials.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    PxIpcAuthError,
    PxIpcClient,
    PxIpcConnectionError,
    PxIpcError,
    normalise_host,
)
from .const import (
    CONF_STREAM,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    STREAM_MAIN,
    STREAM_SUB,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): TextSelector(),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_STREAM, default=STREAM_MAIN): SelectSelector(
            SelectSelectorConfig(
                options=[STREAM_MAIN, STREAM_SUB],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="stream",
            )
        ),
    }
)


async def _probe(
    hass: HomeAssistant, host: str, port: int, username: str, password: str
) -> dict[str, Any]:
    """Log in and read device info; raises whatever the client raises."""
    session = async_get_clientsession(hass)
    client = PxIpcClient(session, host, username, password, port=port)
    return await client.async_test_connection()


class PxIpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host, typed_port = normalise_host(user_input[CONF_HOST])
            port = typed_port or int(user_input.get(CONF_PORT, DEFAULT_PORT))
            username = user_input.get(CONF_USERNAME, DEFAULT_USERNAME).strip()
            password = user_input[CONF_PASSWORD]

            if not host:
                # An empty field must not be reported as a network failure.
                errors["base"] = "invalid_host"
            else:
                try:
                    info = await _probe(self.hass, host, port, username, password)
                except PxIpcAuthError as err:
                    _LOGGER.warning("PX IPC: %s rejected the credentials (%s)", host, err)
                    errors["base"] = "invalid_auth"
                except PxIpcConnectionError as err:
                    # Say exactly what failed, which host:port and why. Without
                    # this the UI only shows "cannot connect", which is the same
                    # message for a typo, a firewall and a wrong subnet.
                    _LOGGER.warning(
                        "PX IPC: cannot reach %s:%s (%s)", host, port, err
                    )
                    errors["base"] = "cannot_connect"
                except PxIpcError as err:
                    _LOGGER.warning("PX IPC: %s answered unexpectedly (%s)", host, err)
                    errors["base"] = "unknown"
                except Exception:  # noqa: BLE001 - never let a flow die with a traceback
                    _LOGGER.exception("PX IPC: unexpected error probing %s", host)
                    errors["base"] = "unknown"
                else:
                    # devId is the camera's stable serial; it survives
                    # re-addressing, so a new IP does not create a duplicate.
                    serial = str(info.get("devId") or host)
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})

                    title = str(info.get("devName") or "PX IPC")
                    return self.async_create_entry(
                        title=f"{title} ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_STREAM: user_input.get(CONF_STREAM, STREAM_MAIN),
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Credentials changed on the camera."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _probe(
                    self.hass,
                    entry.data[CONF_HOST],
                    entry.data.get(CONF_PORT, DEFAULT_PORT),
                    user_input.get(CONF_USERNAME, entry.data[CONF_USERNAME]),
                    user_input[CONF_PASSWORD],
                )
            except PxIpcAuthError:
                errors["base"] = "invalid_auth"
            except PxIpcConnectionError:
                errors["base"] = "cannot_connect"
            except PxIpcError:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input.get(
                            CONF_USERNAME, entry.data[CONF_USERNAME]
                        ),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME, default=entry.data[CONF_USERNAME]
                    ): TextSelector(),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PxIpcOptionsFlow()


class PxIpcOptionsFlow(OptionsFlow):
    """Stream and polling preferences."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_STREAM,
                        default=options.get(
                            CONF_STREAM,
                            self.config_entry.data.get(CONF_STREAM, STREAM_MAIN),
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[STREAM_MAIN, STREAM_SUB],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="stream",
                        )
                    ),
                }
            ),
        )
