"""Config flow for the Open-M2M integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import is_api_success_payload
from .const import (
    API_FIELD_APIKEY,
    BASE_URL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ENDPOINT_ACCOUNT_INFO,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class CannotConnect(Exception):
    """Error to indicate we cannot reach Open-M2M."""


class InvalidAuth(Exception):
    """Error to indicate the supplied API key was rejected."""


async def _validate_api_key(
    session: aiohttp.ClientSession, api_key: str
) -> dict[str, Any]:
    """Call ``GetAccountInfo`` to verify the API key.

    Raises:
        InvalidAuth: API key was rejected (HTTP 401/403 or API-level error).
        CannotConnect: transport, timeout, or unexpected payload errors.
    """
    url = f"{BASE_URL}{ENDPOINT_ACCOUNT_INFO}"
    data = {API_FIELD_APIKEY: api_key}
    try:
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            async with session.post(url, data=data) as resp:
                if resp.status in (401, 403):
                    raise InvalidAuth
                if resp.status >= 400:
                    raise CannotConnect
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as err:
                    _LOGGER.debug("Unexpected response from Open-M2M: %s", err)
                    raise CannotConnect from err
    except TimeoutError as err:
        raise CannotConnect from err
    except aiohttp.ClientError as err:
        _LOGGER.debug("Open-M2M client error: %s", err)
        raise CannotConnect from err

    # OpenAPI documents ``APIstatus`` / ``APIcode`` (1000 = success); accept legacy
    # ``status`` keys via ``is_api_success_payload`` (see ``api.py``).
    if not isinstance(payload, dict):
        return {}
    if not is_api_success_payload(payload):
        raise InvalidAuth
    return payload


class OpenM2MConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Open-M2M."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key: str = user_input[CONF_API_KEY].strip()
            session = async_get_clientsession(self.hass)

            try:
                await _validate_api_key(session, api_key)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - surfaced as generic error to UI
                _LOGGER.exception("Unexpected error validating Open-M2M API key")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(api_key)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Open-M2M",
                    data={CONF_API_KEY: api_key},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
