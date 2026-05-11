"""Sensor platform for Open-M2M account, SIMs, and subscriptions."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OpenM2MCoordinator, OpenM2MCoordinatorData

_LOGGER = logging.getLogger(__name__)


def _client_info_for_attrs(account: dict[str, Any]) -> dict[str, Any] | None:
    """Match coordinator client-info key variants for balance debug attributes."""
    for key in ("ClientInfo", "client_info", "clientInfo", "Clientinfo"):
        ci = account.get(key)
        if isinstance(ci, dict):
            return ci
    return None


def _slug_suffix(value: str, *, max_len: int = 32) -> str:
    """Build a stable object-id suffix from ICCID or subscription id."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return cleaned[:max_len] if cleaned else "sim"


def _sim_iccid(sim: dict[str, Any]) -> str | None:
    for key in ("ICCID", "iccid"):
        v = sim.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    sid = sim.get("subscription_id")
    if sid is not None:
        return f"sub_{sid}"
    return None


def _sim_usage_kb(sim: dict[str, Any]) -> float | None:
    """Return usage in KB if the list/detail payload exposes a recognizable field."""
    direct_keys = (
        "usage_kb",
        "used_kb",
        "total_usage_kb",
        "data_usage_kb",
        "usage_total_kb",
        "used_data_kb",
        "volume_used_kb",
        "data_used_kb",
    )
    for key in direct_keys:
        val = sim.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    for nest_key in ("usage", "Usage", "stats", "Stats", "cdr", "CDR"):
        nested = sim.get(nest_key)
        if isinstance(nested, dict):
            for key in ("kb", "total_kb", "usage_kb", "used_kb", "total_usage_kb"):
                val = nested.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
    return None


def _product_info(sim: dict[str, Any]) -> dict[str, Any]:
    pi = sim.get("Productinfo") or sim.get("productinfo") or sim.get("product_info")
    return pi if isinstance(pi, dict) else {}


def _data_included_mb(sim: dict[str, Any]) -> int | None:
    prod = _product_info(sim)
    val = prod.get("data_included")
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Open-M2M sensors for this config entry."""
    coordinator: OpenM2MCoordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        OpenM2MAccountBalanceSensor(coordinator, entry),
        OpenM2MSimCountSensor(coordinator, entry),
        OpenM2MSubscriptionCountSensor(coordinator, entry),
        OpenM2MSubscriptionsAggregateRemainingSensor(coordinator, entry),
    ]

    data = coordinator.data
    sims = data.get("sims", []) if data else []
    for sim in sims:
        iccid = _sim_iccid(sim)
        if not iccid:
            _LOGGER.debug("Skipping SIM without ICCID/subscription_id: %s", sim)
            continue
        suffix = _slug_suffix(iccid)
        entities.append(
            OpenM2MSimSubscriptionSensor(coordinator, entry, sim, iccid, suffix)
        )
        if _sim_usage_kb(sim) is not None:
            entities.append(
                OpenM2MSimDataUsageSensor(coordinator, entry, sim, iccid, suffix)
            )
        elif _data_included_mb(sim) is not None:
            entities.append(
                OpenM2MSimPlanAllowanceSensor(coordinator, entry, sim, iccid, suffix)
            )

    subs = (data.get("subscriptions") if data else None) or []
    for sub in subs:
        if not isinstance(sub, dict) or not sub.get("subscription_id"):
            continue
        sid = str(sub["subscription_id"])
        slug = _slug_suffix(f"sub_{sid}")
        entities.extend(
            _build_subscription_sensors(coordinator, entry, sub, slug)
        )

    dbs = (data.get("databundles") if data else None) or []
    for db_row in dbs:
        if not isinstance(db_row, dict):
            continue
        db_slug = _databundle_entity_slug(db_row)
        entities.extend(
            _build_databundle_sensors(coordinator, entry, db_row, db_slug)
        )

    async_add_entities(entities)


def _databundle_match_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Stable tuple to match a coordinator databundle row across refreshes."""
    return (
        str(row.get("device_identifier") or ""),
        str(row.get("databundle_id") if row.get("databundle_id") is not None else ""),
        str(row.get("volumegroup_id") if row.get("volumegroup_id") is not None else ""),
    )


def _databundle_entity_slug(row: dict[str, Any]) -> str:
    """Short slug for unique_id suffix (per device + databundle id / fallback)."""
    dev = str(row.get("device_identifier") or "x")
    dbid = row.get("databundle_id")
    if dbid is not None and str(dbid).strip():
        return _slug_suffix(f"{dev}_db_{dbid}")
    vg = row.get("volumegroup_id")
    lid = row.get("linked_subscription_id")
    return _slug_suffix(f"{dev}_vg{vg}_{lid}")


def _databundle_device_info(
    coordinator: OpenM2MCoordinator,
    entry: ConfigEntry,
    row: dict[str, Any],
) -> DeviceInfo:
    """Same ``DeviceInfo`` as the linked SIM when merged; else subscription hub device."""
    iccid_raw = row.get("iccid")
    iccid = iccid_raw.strip() if isinstance(iccid_raw, str) else None
    merged = bool(row.get("merged_onto_sim"))
    if merged and iccid:
        sim_row: dict[str, Any] | None = None
        data = coordinator.data
        if data:
            ic_up = iccid.strip().upper()
            for s in data.get("sims", []):
                if not isinstance(s, dict):
                    continue
                sic = _sim_iccid(s)
                if isinstance(sic, str) and sic.strip().upper() == ic_up:
                    sim_row = s
                    break
        sim = sim_row or {}
        name = sim.get("description") if isinstance(sim.get("description"), str) else None
        product = _product_info(sim)
        model = (
            product.get("description")
            if isinstance(product.get("description"), str)
            else None
        )
        return DeviceInfo(
            identifiers={(DOMAIN, iccid)},
            name=name or f"SIM {iccid}",
            manufacturer="Open-M2M",
            model=model or "SIM",
            via_device=(DOMAIN, entry.entry_id),
        )

    ident = row.get("device_identifier")
    if not isinstance(ident, str) or not ident:
        ident = f"sub_{row.get('linked_subscription_id') or 'unknown'}"
    sub_row: dict[str, Any] | None = None
    lid = row.get("linked_subscription_id")
    if lid is not None and coordinator.data:
        for s in coordinator.data.get("subscriptions", []):
            if isinstance(s, dict) and str(s.get("subscription_id")) == str(lid):
                sub_row = s
                break
    sub = sub_row or {}
    name_bits: list[str] = []
    if isinstance(sub.get("sim_description"), str):
        name_bits.append(sub["sim_description"])
    if isinstance(sub.get("product_name"), str):
        name_bits.append(sub["product_name"])
    dev_name = (
        " / ".join(name_bits) if name_bits else f"Subscription {lid or ident}"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, ident)},
        name=dev_name,
        manufacturer="Open-M2M",
        model="Subscription",
        via_device=(DOMAIN, entry.entry_id),
    )


def _build_databundle_sensors(
    coordinator: OpenM2MCoordinator,
    entry: ConfigEntry,
    row: dict[str, Any],
    slug: str,
) -> list[SensorEntity]:
    """Per linked databundle: status, start, expire, monthly price (best-effort)."""
    match = _databundle_match_key(row)
    dbid = row.get("databundle_id")
    label = str(dbid) if dbid is not None else slug
    return [
        OpenM2MDatabundleStatusSensor(coordinator, entry, row, slug, label, match),
        OpenM2MDatabundleStartSensor(coordinator, entry, row, slug, label, match),
        OpenM2MDatabundleExpireSensor(coordinator, entry, row, slug, label, match),
        OpenM2MDatabundleMonthlySensor(coordinator, entry, row, slug, label, match),
    ]


def _build_subscription_sensors(
    coordinator: OpenM2MCoordinator,
    entry: ConfigEntry,
    sub: dict[str, Any],
    slug: str,
) -> list[SensorEntity]:
    """Create subscription-scoped sensors (portal + bundle + usage)."""
    sid = str(sub["subscription_id"])
    label = sub.get("iccid") or f"sub_{sid}"
    return [
        OpenM2MSubscriptionPortalStatusSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionDataUsedSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionDataAllowanceSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionBundleSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionAutoRenewSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionExpireSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionStartSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionMsisdnSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionIpSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionHostnameSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionRadiusStatusSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionProductDescriptionSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionMonthlySensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionDataproductIncludedSensor(coordinator, entry, sub, slug, label),
        OpenM2MSubscriptionImeiLockSensor(coordinator, entry, sub, slug, label),
    ]


class OpenM2MBaseSensor(CoordinatorEntity[OpenM2MCoordinator], SensorEntity):
    """Common base for Open-M2M sensors."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry


class OpenM2MAccountBalanceSensor(OpenM2MBaseSensor):
    """Account balance / saldo from ``GetAccountInfo``."""

    _attr_icon = "mdi:cash"
    _attr_native_unit_of_measurement = None

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_account_balance"
        self._attr_name = "Open-M2M account balance"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Open-M2M",
            manufacturer="Open-M2M",
            model="Account",
        )

    @property
    def native_value(self) -> float | str | None:
        data: OpenM2MCoordinatorData | None = self.coordinator.data
        if not data:
            return None
        bal = data.get("account_balance")
        if bal is None:
            return None
        return bal

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        account = data.get("account", {})
        attrs: dict[str, Any] = {}
        ci = _client_info_for_attrs(account)
        if isinstance(ci, dict):
            attrs["client_info"] = ci
        braw = data.get("account_balance_raw")
        if braw is not None:
            attrs["account_balance_raw"] = braw
        if data.get("account_balance") is None:
            attrs["raw_account_keys"] = sorted(str(k) for k in account.keys())
        return attrs


class OpenM2MSimCountSensor(OpenM2MBaseSensor):
    """Number of SIMs returned by ``GetSIMs``."""

    _attr_icon = "mdi:sim"
    _attr_native_unit_of_measurement = "SIMs"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sim_count"
        self._attr_name = "Open-M2M SIM count"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Open-M2M",
            manufacturer="Open-M2M",
            model="Account",
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if not data:
            return None
        return len(data.get("sims", []))


class OpenM2MSubscriptionCountSensor(OpenM2MBaseSensor):
    """Count of subscriptions from ``GetSubscriptions``."""

    _attr_icon = "mdi:card-account-details"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_subscription_count"
        self._attr_name = "Open-M2M subscription count"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Open-M2M",
            manufacturer="Open-M2M",
            model="Account",
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if not data:
            return None
        return len(data.get("subscriptions", []))


class OpenM2MSubscriptionsAggregateRemainingSensor(OpenM2MBaseSensor):
    """Sum of (allowance − used) across subscriptions when both are known (bytes)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_subscriptions_data_remaining"
        self._attr_name = "Open-M2M subscriptions data remaining (aggregate)"
        self.entity_description = SensorEntityDescription(
            key="subscriptions_aggregate_remaining",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Open-M2M",
            manufacturer="Open-M2M",
            model="Account",
        )

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if not data:
            return None
        val = data.get("subscriptions_aggregate_remaining_bytes")
        return int(val) if isinstance(val, int) else None


class OpenM2MSimSensorBase(OpenM2MBaseSensor):
    """Sensor tied to a single SIM row."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sim: dict[str, Any],
        iccid: str,
        slug_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._iccid = iccid
        self._slug_suffix = slug_suffix
        self._sim_static = dict(sim)

    def _current_sim(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        for row in data.get("sims", []):
            if not isinstance(row, dict):
                continue
            if _sim_iccid(row) == self._iccid:
                return row
        return None

    @property
    def device_info(self) -> DeviceInfo:
        sim = self._current_sim() or self._sim_static
        name = sim.get("description") if isinstance(sim.get("description"), str) else None
        product = _product_info(sim)
        model = product.get("description") if isinstance(product.get("description"), str) else None
        return DeviceInfo(
            identifiers={(DOMAIN, self._iccid)},
            name=name or f"SIM {self._iccid}",
            manufacturer="Open-M2M",
            model=model or "SIM",
            via_device=(DOMAIN, self._entry.entry_id),
        )


class OpenM2MSimSubscriptionSensor(OpenM2MSimSensorBase):
    """Subscription / lifecycle status for a SIM."""

    _attr_icon = "mdi:sim-outline"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sim: dict[str, Any],
        iccid: str,
        slug_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, sim, iccid, slug_suffix)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_subscription_status"
        self._attr_name = f"Open-M2M SIM {iccid} subscription status"

    @property
    def native_value(self) -> str | None:
        sim = self._current_sim()
        if not sim:
            return None
        status = sim.get("subscription_status") or sim.get("status") or sim.get(
            "SIMstatus"
        )
        if isinstance(status, str) and status.strip():
            return status.strip()
        archived = sim.get("archived")
        if isinstance(archived, bool):
            return "archived" if archived else "not_archived"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sim = self._current_sim() or self._sim_static
        attrs: dict[str, Any] = {"iccid": self._iccid}
        for key in ("MSISDN", "msisdn", "IMSI", "imsi", "subscription_id", "description"):
            if key in sim:
                attrs[key] = sim[key]
        prod = _product_info(sim)
        if prod:
            attrs["productinfo"] = prod
        return attrs


class OpenM2MSimDataUsageSensor(OpenM2MSimSensorBase):
    """Observed data usage when the SIM list exposes a usage field (KB)."""

    _attr_icon = "mdi:chart-bell-curve"
    _attr_native_unit_of_measurement = "kB"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sim: dict[str, Any],
        iccid: str,
        slug_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, sim, iccid, slug_suffix)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_data_usage_kb"
        self._attr_name = f"Open-M2M SIM {iccid} data usage"

    @property
    def native_value(self) -> float | str | None:
        sim = self._current_sim()
        if not sim:
            return None
        kb = _sim_usage_kb(sim)
        if kb is None:
            return None
        return kb


class OpenM2MSimPlanAllowanceSensor(OpenM2MSimSensorBase):
    """Included data from ``Productinfo.data_included`` when usage is absent."""

    _attr_icon = "mdi:package-variant"
    _attr_native_unit_of_measurement = "MB"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sim: dict[str, Any],
        iccid: str,
        slug_suffix: str,
    ) -> None:
        super().__init__(coordinator, entry, sim, iccid, slug_suffix)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_plan_data_included"
        self._attr_name = f"Open-M2M SIM {iccid} plan data included"

    @property
    def native_value(self) -> int | str | None:
        sim = self._current_sim()
        if not sim:
            return None
        mb = _data_included_mb(sim)
        if mb is None:
            return None
        return mb


class OpenM2MSubscriptionSensorBase(OpenM2MBaseSensor):
    """Sensor tied to one normalized subscription row."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._subscription_id = str(sub["subscription_id"])
        self._slug_suffix = slug_suffix
        self._label = label
        self._sub_static = dict(sub)

    def _current_sub(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        for row in data.get("subscriptions", []):
            if isinstance(row, dict) and str(row.get("subscription_id")) == self._subscription_id:
                return row
        return None

    def _subscription_device_info(self) -> DeviceInfo:
        sub = self._current_sub() or self._sub_static
        ident = sub.get("device_identifier") or self._sub_static.get("device_identifier")
        if not isinstance(ident, str) or not ident:
            ident = f"sub_{self._subscription_id}"
        name_bits: list[str] = []
        if isinstance(sub.get("sim_description"), str):
            name_bits.append(sub["sim_description"])
        if isinstance(sub.get("product_name"), str):
            name_bits.append(sub["product_name"])
        dev_name = " / ".join(name_bits) if name_bits else f"Subscription {self._subscription_id}"
        return DeviceInfo(
            identifiers={(DOMAIN, ident)},
            name=dev_name,
            manufacturer="Open-M2M",
            model="Subscription",
            via_device=(DOMAIN, self._entry.entry_id),
        )

    @property
    def device_info(self) -> DeviceInfo:
        return self._subscription_device_info()


class OpenM2MSubscriptionPortalStatusSensor(OpenM2MSubscriptionSensorBase):
    """Status from ``GetSubscriptions`` / optional ``GetSubscriptionInfo``."""

    _attr_icon = "mdi:list-status"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_subscription_status"
        self._attr_name = f"Open-M2M subscription {label} portal status"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        st = sub.get("status")
        return st if isinstance(st, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self._current_sub() or self._sub_static
        attrs: dict[str, Any] = {
            "subscription_id": self._subscription_id,
            "merged_onto_sim": sub.get("merged_onto_sim"),
        }
        for key in (
            "iccid",
            "msisdn",
            "imsi",
            "ip",
            "hostname",
            "radius_status",
            "sim_description",
            "product_description",
            "product_name",
            "monthly",
        ):
            val = sub.get(key)
            if val is not None:
                attrs[key] = val
        lim = sub.get("limit_data_mb")
        if isinstance(lim, (int, float)):
            attrs["limit_data_mb"] = lim
        pdi = sub.get("product_data_included_mb")
        if isinstance(pdi, (int, float)):
            attrs["product_data_included_mb"] = pdi
        dps = sub.get("dataproduct_summaries")
        if isinstance(dps, list) and dps:
            attrs["dataproduct_summaries"] = dps
        return attrs


class OpenM2MSubscriptionDataUsedSensor(OpenM2MSubscriptionSensorBase):
    """Aggregated data used from matched ``VolumeGroup`` rows (bytes)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_data_used"
        self._attr_name = f"Open-M2M subscription {label} data used"
        self.entity_description = SensorEntityDescription(
            key="subscription_data_used",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )

    @property
    def native_value(self) -> int | None:
        sub = self._current_sub()
        if not sub:
            return None
        val = sub.get("data_used_bytes")
        return int(val) if isinstance(val, int) else None


class OpenM2MSubscriptionDataAllowanceSensor(OpenM2MSubscriptionSensorBase):
    """Allowance / cap from volume totals or ``limit_data`` (bytes)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_data_allowance"
        self._attr_name = f"Open-M2M subscription {label} data allowance"
        self.entity_description = SensorEntityDescription(
            key="subscription_data_allowance",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )

    @property
    def native_value(self) -> int | None:
        sub = self._current_sub()
        if not sub:
            return None
        val = sub.get("data_allowance_bytes")
        return int(val) if isinstance(val, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self._current_sub() or self._sub_static
        attrs: dict[str, Any] = {}
        for key in (
            "limit_data_mb",
            "product_data_included_mb",
            "product_data_included_bytes",
            "dataproduct_data_included_mb",
            "dataproduct_data_included_bytes",
        ):
            val = sub.get(key)
            if val is not None:
                attrs[key] = val
        dps = sub.get("dataproduct_summaries")
        if isinstance(dps, list) and dps:
            attrs["dataproduct_summaries"] = dps
        return attrs


class OpenM2MSubscriptionBundleSensor(OpenM2MSubscriptionSensorBase):
    """Primary databundle product name(s)."""

    _attr_icon = "mdi:package-variant-closed"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_bundle"
        self._attr_name = f"Open-M2M subscription {label} bundle"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        names = sub.get("bundle_names")
        if isinstance(names, list) and names:
            return ", ".join(str(n) for n in names if n)
        primary = sub.get("bundle_primary_name")
        if isinstance(primary, str) and primary.strip():
            return primary.strip()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self._current_sub() or self._sub_static
        out: dict[str, Any] = {}
        bs = sub.get("bundle_summaries")
        if isinstance(bs, list):
            out["bundle_summaries"] = bs
        dps = sub.get("dataproduct_summaries")
        if isinstance(dps, list) and dps:
            out["dataproduct_summaries"] = dps
        return out


class OpenM2MSubscriptionAutoRenewSensor(OpenM2MSubscriptionSensorBase):
    """Auto-renewal flag when present on the subscription row."""

    _attr_icon = "mdi:autorenew"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_auto_renew"
        self._attr_name = f"Open-M2M subscription {label} auto renew"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        ar = sub.get("auto_renewal")
        if isinstance(ar, bool):
            return "on" if ar else "off"
        return None


class OpenM2MSubscriptionExpireSensor(OpenM2MSubscriptionSensorBase):
    """Subscription expiry as UTC timestamp when ``expire_date`` is Unix time."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_expire"
        self._attr_name = f"Open-M2M subscription {label} expire time"
        self.entity_description = SensorEntityDescription(
            key="subscription_expire",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        sub = self._current_sub()
        if not sub:
            return None
        exp = sub.get("expire_date")
        if not isinstance(exp, int) or exp <= 0:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)


class OpenM2MSubscriptionStartSensor(OpenM2MSubscriptionSensorBase):
    """Subscription start as UTC timestamp when ``start_date`` is Unix time."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_start"
        self._attr_name = f"Open-M2M subscription {label} start time"
        self.entity_description = SensorEntityDescription(
            key="subscription_start",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        sub = self._current_sub()
        if not sub:
            return None
        start = sub.get("start_date")
        if not isinstance(start, int) or start <= 0:
            return None
        return datetime.fromtimestamp(start, tz=timezone.utc)


class OpenM2MSubscriptionMsisdnSensor(OpenM2MSubscriptionSensorBase):
    """MSISDN from subscription row / SIM merge."""

    _attr_icon = "mdi:cellphone"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_msisdn"
        self._attr_name = f"Open-M2M subscription {label} MSISDN"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        m = sub.get("msisdn")
        return m if isinstance(m, str) and m.strip() else None


class OpenM2MSubscriptionIpSensor(OpenM2MSubscriptionSensorBase):
    """Last-known IP from ``GetSubscriptions`` / merged info."""

    _attr_icon = "mdi:ip-network"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_ip"
        self._attr_name = f"Open-M2M subscription {label} IP"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        ip = sub.get("ip")
        return ip if isinstance(ip, str) and ip.strip() else None


class OpenM2MSubscriptionHostnameSensor(OpenM2MSubscriptionSensorBase):
    """Hostname from portal subscription row / merged info."""

    _attr_icon = "mdi:dns"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_hostname"
        self._attr_name = f"Open-M2M subscription {label} hostname"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        h = sub.get("hostname")
        return h if isinstance(h, str) and h.strip() else None


class OpenM2MSubscriptionRadiusStatusSensor(OpenM2MSubscriptionSensorBase):
    """RADIUS / session status when the API exposes it."""

    _attr_icon = "mdi:server-network"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_radius_status"
        self._attr_name = f"Open-M2M subscription {label} RADIUS status"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        r = sub.get("radius_status")
        return r if isinstance(r, str) and r.strip() else None


class OpenM2MSubscriptionProductDescriptionSensor(OpenM2MSubscriptionSensorBase):
    """Product / plan description from the subscription row or merged product info."""

    _attr_icon = "mdi:text-box-outline"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_product_description"
        self._attr_name = f"Open-M2M subscription {label} product description"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        for key in ("product_description", "product_name"):
            v = sub.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None


class OpenM2MSubscriptionMonthlySensor(OpenM2MSubscriptionSensorBase):
    """Whether the plan is monthly (bool from row or ``GetSubscriptionInfo``)."""

    _attr_icon = "mdi:calendar-month"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_monthly"
        self._attr_name = f"Open-M2M subscription {label} monthly plan"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        m = sub.get("monthly")
        if isinstance(m, bool):
            return "yes" if m else "no"
        return None


class OpenM2MSubscriptionDataproductIncludedSensor(OpenM2MSubscriptionSensorBase):
    """Included data from first parsed ``DataProduct`` on databundle rows (bytes)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_portal_dataproduct_data_included"
        self._attr_name = f"Open-M2M subscription {label} DataProduct data included"
        self.entity_description = SensorEntityDescription(
            key="subscription_dataproduct_data_included",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        )

    @property
    def native_value(self) -> int | None:
        sub = self._current_sub()
        if not sub:
            return None
        val = sub.get("dataproduct_data_included_bytes")
        return int(val) if isinstance(val, int) else None


class OpenM2MDatabundleSensorBase(OpenM2MBaseSensor):
    """Sensor for one linked databundle row (matched across coordinator refreshes)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        row: dict[str, Any],
        slug: str,
        label: str,
        match: tuple[str, str, str],
    ) -> None:
        super().__init__(coordinator, entry)
        self._slug = slug
        self._label = label
        self._match = match
        self._row_static = dict(row)

    def _current_db_row(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data:
            return None
        for db in data.get("databundles") or []:
            if isinstance(db, dict) and _databundle_match_key(db) == self._match:
                return db
        return None

    @property
    def device_info(self) -> DeviceInfo:
        row = self._current_db_row() or self._row_static
        return _databundle_device_info(self.coordinator, self._entry, row)


class OpenM2MDatabundleStatusSensor(OpenM2MDatabundleSensorBase):
    """Portal databundle status; description and ids in attributes."""

    _attr_icon = "mdi:package-variant"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        row: dict[str, Any],
        slug: str,
        label: str,
        match: tuple[str, str, str],
    ) -> None:
        super().__init__(coordinator, entry, row, slug, label, match)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_databundle_status"
        self._attr_name = f"Open-M2M databundle {label} status"

    @property
    def native_value(self) -> str | None:
        db = self._current_db_row()
        if not db:
            return None
        st = db.get("status")
        return st if isinstance(st, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        db = self._current_db_row() or self._row_static
        attrs: dict[str, Any] = {}
        for key in (
            "databundle_id",
            "product_description",
            "volumegroup_id",
            "linked_subscription_id",
            "iccid",
            "source_subscription_id",
            "merged_onto_sim",
        ):
            if key in db and db[key] is not None:
                attrs[key] = db[key]
        return attrs


class OpenM2MDatabundleStartSensor(OpenM2MDatabundleSensorBase):
    """Databundle start time (Unix seconds → UTC timestamp)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        row: dict[str, Any],
        slug: str,
        label: str,
        match: tuple[str, str, str],
    ) -> None:
        super().__init__(coordinator, entry, row, slug, label, match)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_databundle_start"
        self._attr_name = f"Open-M2M databundle {label} start time"
        self.entity_description = SensorEntityDescription(
            key="databundle_start",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        db = self._current_db_row()
        if not db:
            return None
        start = db.get("start_date")
        if not isinstance(start, int) or start <= 0:
            return None
        return datetime.fromtimestamp(start, tz=timezone.utc)


class OpenM2MDatabundleExpireSensor(OpenM2MDatabundleSensorBase):
    """Databundle expiry (Unix seconds → UTC timestamp)."""

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        row: dict[str, Any],
        slug: str,
        label: str,
        match: tuple[str, str, str],
    ) -> None:
        super().__init__(coordinator, entry, row, slug, label, match)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_databundle_expire"
        self._attr_name = f"Open-M2M databundle {label} expire time"
        self.entity_description = SensorEntityDescription(
            key="databundle_expire",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        db = self._current_db_row()
        if not db:
            return None
        exp = db.get("expire_date")
        if not isinstance(exp, int) or exp <= 0:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)


class OpenM2MDatabundleMonthlySensor(OpenM2MDatabundleSensorBase):
    """Monthly charge / fee field from the databundle row (numeric; unit EUR is indicative)."""

    _attr_icon = "mdi:cash"

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        row: dict[str, Any],
        slug: str,
        label: str,
        match: tuple[str, str, str],
    ) -> None:
        super().__init__(coordinator, entry, row, slug, label, match)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_databundle_monthly"
        self._attr_name = f"Open-M2M databundle {label} monthly"
        self._attr_native_unit_of_measurement = "EUR"
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | int | None:
        db = self._current_db_row()
        if not db:
            return None
        m = db.get("monthly")
        if isinstance(m, bool):
            return None
        if isinstance(m, (int, float)):
            return m
        return None


class OpenM2MSubscriptionImeiLockSensor(OpenM2MSubscriptionSensorBase):
    """IMEI lock hint from the portal (diagnostic; disabled by default in the UI)."""

    _attr_icon = "mdi:lock-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: OpenM2MCoordinator,
        entry: ConfigEntry,
        sub: dict[str, Any],
        slug_suffix: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry, sub, slug_suffix, label)
        self._attr_unique_id = f"{entry.entry_id}_{slug_suffix}_diag_imei_lock"
        self._attr_name = f"Open-M2M subscription {label} IMEI lock"

    @property
    def native_value(self) -> str | None:
        sub = self._current_sub()
        if not sub:
            return None
        v = sub.get("imei_lock")
        if isinstance(v, bool):
            return "locked" if v else "unlocked"
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None
