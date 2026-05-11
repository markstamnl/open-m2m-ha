"""The Open-M2M integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .api import OpenM2MClient, OpenM2MError
from .const import DOMAIN
from .coordinator import OpenM2MCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

SERVICE_SUSPEND_SIM = "suspend_sim"
SERVICE_UNSUSPEND_SIM = "unsuspend_sim"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ICCID = "iccid"

ICCID_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ICCID): str,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
    }
)


@dataclass(slots=True)
class OpenM2MRuntimeData:
    """Holds runtime objects attached to ``ConfigEntry.runtime_data``."""

    coordinator: OpenM2MCoordinator


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    if not entries:
        raise HomeAssistantError("No loaded Open-M2M config entries")
    cid = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if cid:
        match = next((e for e in entries if e.entry_id == cid), None)
        if not match:
            raise HomeAssistantError(f"Unknown Open-M2M config entry id: {cid}")
        return match
    if len(entries) > 1:
        raise HomeAssistantError(
            "Multiple Open-M2M accounts are configured; pass config_entry_id in the service data"
        )
    return entries[0]


async def _async_register_services(hass: HomeAssistant) -> None:
    async def async_suspend(call: ServiceCall) -> None:
        entry = _resolve_entry(hass, call)
        iccid = str(call.data[ATTR_ICCID]).strip()
        if not iccid:
            raise HomeAssistantError("iccid must not be empty")
        runtime: OpenM2MRuntimeData = entry.runtime_data
        try:
            await runtime.coordinator.client.async_suspend_sim(iccid)
        except OpenM2MError as err:
            raise HomeAssistantError(str(err)) from err
        await runtime.coordinator.async_request_refresh()

    async def async_unsuspend(call: ServiceCall) -> None:
        entry = _resolve_entry(hass, call)
        iccid = str(call.data[ATTR_ICCID]).strip()
        if not iccid:
            raise HomeAssistantError("iccid must not be empty")
        runtime: OpenM2MRuntimeData = entry.runtime_data
        try:
            await runtime.coordinator.client.async_unsuspend_sim(iccid)
        except OpenM2MError as err:
            raise HomeAssistantError(str(err)) from err
        await runtime.coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_SUSPEND_SIM, async_suspend, schema=ICCID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNSUSPEND_SIM, async_unsuspend, schema=ICCID_SCHEMA
    )


async def _async_unregister_services(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE_SUSPEND_SIM)
    hass.services.async_remove(DOMAIN, SERVICE_UNSUSPEND_SIM)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Open-M2M from a config entry."""
    api_key: str = str(entry.data[CONF_API_KEY]).strip()
    client = OpenM2MClient(hass, api_key)
    coordinator = OpenM2MCoordinator(hass, entry, client)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = OpenM2MRuntimeData(coordinator=coordinator)

    bucket = hass.data.setdefault(DOMAIN, {})
    ref_count: int = int(bucket.get("service_ref_count", 0))
    if ref_count == 0:
        await _async_register_services(hass)
    bucket["service_ref_count"] = ref_count + 1

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and DOMAIN in hass.data:
        bucket = hass.data[DOMAIN]
        current = int(bucket.get("service_ref_count", 0))
        new_val = max(current - 1, 0)
        if new_val == 0:
            await _async_unregister_services(hass)
            bucket.pop("service_ref_count", None)
            if len(bucket) == 0:
                hass.data.pop(DOMAIN, None)
        else:
            bucket["service_ref_count"] = new_val
    return ok
