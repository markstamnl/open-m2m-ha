"""Async Open-M2M portal API client."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_FIELD_APIKEY,
    BASE_URL,
    DEFAULT_TIMEOUT,
    ENDPOINT_ACCOUNT_INFO,
    ENDPOINT_GET_DATABUNDLES,
    ENDPOINT_GET_SUBSCRIPTION_INFO,
    ENDPOINT_GET_SUBSCRIPTIONS,
    ENDPOINT_GET_VOLUME_GROUPS,
    ENDPOINT_SIMS,
    ENDPOINT_SUSPEND_SIM,
    ENDPOINT_UNSUSPEND_SIM,
)

CONTENT_TYPE_FORM: Final[str] = "application/x-www-form-urlencoded"

_LOGGER = logging.getLogger(__name__)


class OpenM2MError(Exception):
    """Base error for Open-M2M API failures."""


class OpenM2MTimeoutError(OpenM2MError):
    """Request exceeded the configured timeout."""


class OpenM2MHTTPError(OpenM2MError):
    """Non-success HTTP status from the portal."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class OpenM2MParseError(OpenM2MError):
    """Response body could not be decoded as JSON."""


class OpenM2MAPIResponseError(OpenM2MError):
    """HTTP 200 JSON payload indicates an API-level failure."""

    def __init__(
        self,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = payload


def is_api_success_payload(payload: dict[str, Any]) -> bool:
    """Return True when the portal JSON indicates success.

    OpenAPI v1.0.7 documents ``APIstatus`` / ``APIcode`` (1000 = success).
    Older samples used ``status``; accept both defensively.
    """
    api_code = payload.get("APIcode")
    if api_code is not None:
        try:
            if int(api_code) != 1000:
                return False
        except (TypeError, ValueError):
            return False

    api_status = str(payload.get("APIstatus", "")).strip().lower()
    if api_status:
        if api_status in {"success", "ok", "true", "1"}:
            return True
        if api_status in {"error", "fail", "failed", "false", "0"}:
            return False

    legacy = str(payload.get("status", "")).strip().lower()
    if legacy:
        return legacy in {"ok", "success", "true", "1"}

    # If neither success nor failure markers exist, treat as success when HTTP was 200.
    return True


class OpenM2MClient:
    """Thin async wrapper around Open-M2M ``POST`` form endpoints."""

    def __init__(self, hass: HomeAssistant, api_key: str) -> None:
        self._hass = hass
        self._api_key = api_key

    async def async_get_account_info(self) -> dict[str, Any]:
        """Call ``GetAccountInfo``."""
        return await self._post_json(ENDPOINT_ACCOUNT_INFO)

    async def async_get_sims(self) -> dict[str, Any]:
        """Call ``GetSIMs`` (list / map of SIMs for the account)."""
        return await self._post_json(ENDPOINT_SIMS)

    async def async_get_subscriptions(self) -> dict[str, Any]:
        """Call ``GetSubscriptions``."""
        return await self._post_json(ENDPOINT_GET_SUBSCRIPTIONS)

    async def async_get_subscription_info(
        self, subscription_id: str | int
    ) -> dict[str, Any]:
        """Call ``GetSubscriptionInfo`` for one subscription id."""
        return await self._post_json(
            ENDPOINT_GET_SUBSCRIPTION_INFO,
            {"subscription_id": str(subscription_id)},
        )

    async def async_get_databundles(self, subscription_id: str | int) -> dict[str, Any]:
        """Call ``GetDatabundles`` (requires ``subscription_id``)."""
        return await self._post_json(
            ENDPOINT_GET_DATABUNDLES,
            {"subscription_id": str(subscription_id)},
        )

    async def async_get_volume_groups(
        self, subscription_id: str | int | None = None
    ) -> dict[str, Any]:
        """Call ``GetVolumeGroups`` (optional ``subscription_id`` filter)."""
        extra: dict[str, Any] | None = None
        if subscription_id is not None:
            extra = {"subscription_id": str(subscription_id)}
        return await self._post_json(ENDPOINT_GET_VOLUME_GROUPS, extra)

    async def async_suspend_sim(self, iccid: str) -> dict[str, Any]:
        """Call ``SuspendSIM``."""
        return await self._post_json(ENDPOINT_SUSPEND_SIM, {"ICCID": iccid})

    async def async_unsuspend_sim(self, iccid: str) -> dict[str, Any]:
        """Call ``UnsuspendSIM``."""
        return await self._post_json(ENDPOINT_UNSUSPEND_SIM, {"ICCID": iccid})

    async def _post_json(
        self,
        endpoint: str,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST ``application/x-www-form-urlencoded`` and parse JSON."""
        session = async_get_clientsession(self._hass)
        url = f"{BASE_URL}{endpoint}"
        data: dict[str, Any] = {API_FIELD_APIKEY: self._api_key}
        if extra_data:
            data.update(extra_data)

        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT):
                async with session.post(
                    url,
                    data=data,
                    headers={"Content-Type": CONTENT_TYPE_FORM},
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        _LOGGER.debug(
                            "Open-M2M HTTP %s for %s: %s",
                            resp.status,
                            endpoint,
                            text[:500],
                        )
                        raise OpenM2MHTTPError(
                            f"HTTP {resp.status} for {endpoint}",
                            status=resp.status,
                        )
                    try:
                        body: Any = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as err:
                        raise OpenM2MParseError(
                            f"Invalid JSON for {endpoint}"
                        ) from err
        except TimeoutError as err:
            raise OpenM2MTimeoutError(f"Timeout calling {endpoint}") from err
        except aiohttp.ClientError as err:
            raise OpenM2MError(f"Client error calling {endpoint}: {err}") from err

        if not isinstance(body, dict):
            raise OpenM2MParseError(f"Expected JSON object for {endpoint}")

        if not is_api_success_payload(body):
            raise OpenM2MAPIResponseError(
                f"API reported failure for {endpoint}",
                payload=body,
            )

        return body
