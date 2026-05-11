"""DataUpdateCoordinator for Open-M2M account + SIM list + subscriptions."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, NotRequired, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenM2MClient, OpenM2MError
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_DATABUNDLES,
    MAX_PRODUCT_INFO_FETCHES,
    MAX_SIM_DETAIL_ENRICHMENT_PER_REFRESH,
    MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES,
)

_LOGGER = logging.getLogger(__name__)

# Concurrency for per-subscription GETs (OpenAPI requires ``subscription_id``).
SUBSCRIPTION_FETCH_CONCURRENCY = 3
# Cap optional ``GetSubscriptionInfo`` calls per refresh to avoid slowdowns.
MAX_SUBSCRIPTION_INFO_CALLS = 10


class UsageTotalsMonth(TypedDict):
    """Normalized ``GetUsageTotals`` / ``CDR`` slice for one calendar month."""

    year: int
    month: int
    total_usage_kb: float


class OpenM2MCoordinatorData(TypedDict):
    """Structured data exposed on ``coordinator.data``."""

    account: dict[str, Any]
    account_balance: float | None
    sims: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]
    subscriptions_aggregate_remaining_bytes: int | None
    databundles: list[dict[str, Any]]
    usage_totals_current: UsageTotalsMonth | None
    usage_totals_previous: UsageTotalsMonth | None
    account_balance_raw: NotRequired[Any]


def _parse_balance_number(val: Any) -> float | None:
    """Parse portal balance values: numbers, or strings with comma decimals / currency."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        s = re.sub(r"^[\s€$£]+|[\s€$£]+$", "", s, flags=re.IGNORECASE)
        s = s.replace("EUR", "").strip()
        if not s:
            return None
        # Both '.' and ',' — assume last separator is decimal (1.234,56 vs 1,234.56).
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s and "." not in s:
            if s.count(",") == 1:
                s = s.replace(",", ".")
            else:
                return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _client_info_dict(account: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve client/account info block with alternate casings."""
    for key in ("ClientInfo", "client_info", "clientInfo", "Clientinfo"):
        ci = account.get(key)
        if isinstance(ci, dict):
            return ci
    return None


def _extract_balance(account: dict[str, Any]) -> tuple[float | None, Any]:
    """Best-effort balance from ``GetAccountInfo``; returns (parsed, raw) for debugging."""
    client_info = _client_info_dict(account)
    if client_info is not None:
        for key in (
            "balance",
            "Balance",
            "saldo",
            "Saldo",
            "credit",
            "Credit",
            "account_balance",
        ):
            if key not in client_info:
                continue
            raw = client_info[key]
            parsed = _parse_balance_number(raw)
            if parsed is not None:
                return (parsed, raw)

    for key in (
        "balance",
        "saldo",
        "Balance",
        "Saldo",
        "credit",
        "Credit",
        "account_balance",
        "AccountBalance",
    ):
        if key not in account:
            continue
        raw = account[key]
        parsed = _parse_balance_number(raw)
        if parsed is not None:
            return (parsed, raw)

    return (None, None)


def _sim_row_nonempty_description(row: dict[str, Any]) -> bool:
    """True when the portal exposes any non-empty SIM label / description field."""
    for key in ("description", "Description", "sim_description", "SimDescription"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _inject_iccid_from_sim_mapping(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """When ``SIMs`` is ``{ id: { ... } }`` and rows omit ``ICCID``, copy digit keys."""
    out: list[dict[str, Any]] = []
    for k, v in mapping.items():
        if not isinstance(v, dict):
            continue
        row = dict(v)
        if not _normalize_iccid(row.get("ICCID") or row.get("iccid")):
            ks = str(k).strip().upper().replace(" ", "")
            if ks.isdigit() and 18 <= len(ks) <= 22:
                row["ICCID"] = ks
        out.append(row)
    return out


def _normalize_sims(sims_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn ``GetSIMs`` JSON into a list of SIM dicts.

    ``SIMs`` may be a **list**, a **single SIM object** (keys such as ``ICCID``),
    or a **map** of id → row. Parse defensively and log odd shapes at debug.
    """
    raw = sims_payload.get("SIMs")
    if raw is None:
        for alt in ("sims", "SIMList", "SimList"):
            if alt in sims_payload:
                raw = sims_payload[alt]
                _LOGGER.debug("Using alternate SIM container key %s", alt)
                break

    if raw is None:
        return []

    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]

    if isinstance(raw, dict):
        # One SIM row: ICCID / subscription_id, or portal SIM name + MSISDN/IMSI (no ICCID key).
        if any(k in raw for k in ("ICCID", "iccid", "subscription_id")):
            return [raw]
        if any(k in raw for k in ("description", "Description", "sim_description", "SimDescription")) and any(
            k in raw for k in ("MSISDN", "msisdn", "IMSI", "imsi")
        ):
            return [raw]
        values = [v for v in raw.values() if isinstance(v, dict)]
        if values:
            missing_all_iccid = all(
                _normalize_iccid(v.get("ICCID") or v.get("iccid")) is None
                for v in values
            )
            if missing_all_iccid:
                injected = _inject_iccid_from_sim_mapping(raw)
                if injected:
                    _LOGGER.debug(
                        "GetSIMs: flattened SIMs id→row map (%s row(s)); ICCID from keys where missing",
                        len(injected),
                    )
                    return injected
                _LOGGER.debug(
                    "GetSIMs: SIMs dict values lack ICCID and keys are not usable as ICCID; keys=%s",
                    list(raw.keys())[:20],
                )
            return values
        _LOGGER.debug(
            "GetSIMs returned SIMs object without recognizable SIM dict rows; keys=%s",
            list(raw.keys())[:20],
        )
        return []

    _LOGGER.debug(
        "Unexpected SIMs payload type %s; ignoring", type(raw).__name__
    )
    return []


def _normalize_container_list(
    payload: dict[str, Any],
    primary: str,
    alternates: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Parse an API child that may be a dict, list, or missing."""
    raw: Any = payload.get(primary)
    if raw is None:
        for alt in alternates:
            if alt in payload:
                raw = payload[alt]
                _LOGGER.debug("Using alternate container key %s for %s", alt, primary)
                break
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        if any(
            k in raw
            for k in (
                "subscription_id",
                "ICCID",
                "iccid",
                "volumegroup",
                "databundle_id",
            )
        ):
            return [raw]
        vals = [v for v in raw.values() if isinstance(v, dict)]
        return vals
    _LOGGER.debug("Unexpected container type for %s: %s", primary, type(raw).__name__)
    return []


_SUBSCRIPTION_LIST_KEYS: tuple[str, ...] = (
    "Subscriptions",
    "subscriptions",
    "Subscription",
    "subscription",
    "SubscriptionList",
    "subscription_list",
    "SubscriptionsList",
    "subscriptions_list",
    "Rows",
    "rows",
    "Items",
    "items",
    "List",
    "list",
)

_SUBSCRIPTION_PAYLOAD_WRAPPERS: tuple[str, ...] = (
    "data",
    "result",
    "Result",
    "Data",
    "payload",
    "response",
    "Response",
    "body",
)

_SUBSCRIPTION_ROW_MARKERS: tuple[str, ...] = (
    "subscription_id",
    "SubscriptionID",
    "subscriptionId",
    "ICCID",
    "iccid",
    "MSISDN",
    "msisdn",
    "product_description",
    "ProductDescription",
    "sim_description",
    "SimDescription",
    "IMSI",
    "imsi",
    "volumegroup",
    "databundle_id",
)


def _looks_like_subscription_dict(d: dict[str, Any]) -> bool:
    """True if ``d`` resembles one ``GetSubscriptions`` row (object vs id→row map)."""
    return any(m in d for m in _SUBSCRIPTION_ROW_MARKERS)


def _subscription_payload_bases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Unwrap common outer keys so list fields may live one level down."""
    bases: list[dict[str, Any]] = [payload]
    for w in _SUBSCRIPTION_PAYLOAD_WRAPPERS:
        inner = payload.get(w)
        if isinstance(inner, dict):
            bases.append(inner)
    return bases


def _inject_subscription_id_from_mapping_key(
    mapping: dict[str, Any],
) -> list[dict[str, Any]]:
    """When API returns ``{ "<id>": { ... } }`` without ``subscription_id`` inside each row."""
    out: list[dict[str, Any]] = []
    for k, v in mapping.items():
        if not isinstance(v, dict):
            continue
        row = dict(v)
        if row.get("subscription_id") is None:
            ks = str(k).strip()
            if ks.isdigit():
                row["subscription_id"] = int(ks)
            elif ks:
                row["subscription_id"] = ks
        out.append(row)
    return out


def _normalize_subscription_container(base: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse ``GetSubscriptions``-shaped JSON into subscription dict rows."""
    raw: Any = None
    used_key: str | None = None
    for pk in _SUBSCRIPTION_LIST_KEYS:
        if pk in base:
            raw = base[pk]
            used_key = pk
            break
    if raw is None:
        return []

    if isinstance(raw, list):
        rows = [x for x in raw if isinstance(x, dict)]
        _ensure_subscription_ids(rows)
        return [r for r in rows if r.get("subscription_id") not in (None, "")]

    if isinstance(raw, dict):
        if _looks_like_subscription_dict(raw):
            row = dict(raw)
            _ensure_subscription_id(row)
            return [row] if row.get("subscription_id") not in (None, "") else []

        vals = [v for v in raw.values() if isinstance(v, dict)]
        if vals:
            if all(
                v.get("subscription_id") is None
                and v.get("SubscriptionID") is None
                for v in vals
            ):
                injected = _inject_subscription_id_from_mapping_key(raw)
                _ensure_subscription_ids(injected)
                if injected:
                    return [
                        r
                        for r in injected
                        if r.get("subscription_id") not in (None, "")
                    ]
            _ensure_subscription_ids(vals)
            return [r for r in vals if r.get("subscription_id") not in (None, "")]

        _LOGGER.debug(
            "GetSubscriptions key %s had dict without recognizable subscription rows; keys=%s",
            used_key,
            list(raw.keys())[:25],
        )
        return []

    _LOGGER.debug(
        "Unexpected GetSubscriptions container type for key %s: %s",
        used_key,
        type(raw).__name__,
    )
    return []


def _ensure_subscription_id(row: dict[str, Any]) -> None:
    """Normalize alternate id keys onto ``subscription_id``."""
    if row.get("subscription_id") not in (None, ""):
        return
    for alt in ("SubscriptionID", "subscriptionId", "sub_id", "SubId"):
        v = row.get(alt)
        if v is not None and str(v).strip():
            row["subscription_id"] = v
            return


def _ensure_subscription_ids(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        _ensure_subscription_id(r)


def _normalize_subscription_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect subscription rows from top-level and common wrapper dicts.

    Merges rows from every payload level (e.g. top-level ``Subscriptions`` and
    ``data.Subscriptions``). Deduplicates by ``subscription_id`` so the same
    subscription is not counted twice when the API echoes list and object shapes.
    """
    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    for base in _subscription_payload_bases(payload):
        for row in _normalize_subscription_container(base):
            _ensure_subscription_id(row)
            sid = row.get("subscription_id")
            if sid is None or str(sid).strip() == "":
                continue
            key = str(sid).strip()
            if key in seen_ids:
                continue
            seen_ids.add(key)
            merged.append(row)
    return merged


def _mb_pair_to_bytes(used_mb: float | None, volume_mb: float | None) -> tuple[int | None, int | None]:
    """Convert Open-M2M ``VolumeGroup`` ``used`` / ``volume`` (MB per examples) to bytes."""
    factor = 1024 * 1024
    u_b: int | None = None
    v_b: int | None = None
    if used_mb is not None:
        u_b = int(round(float(used_mb) * factor))
    if volume_mb is not None:
        v_b = int(round(float(volume_mb) * factor))
    return u_b, v_b


def _volume_groups_map(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index ``GetVolumeGroups`` rows by ``volumegroup`` id."""
    rows = _normalize_container_list(payload, "VolumeGroups", ("volumeGroups", "VolumeGroup"))
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        vg = row.get("volumegroup")
        if vg is None:
            continue
        try:
            out[int(vg)] = row
        except (TypeError, ValueError):
            continue
    return out


_DATABUNDLE_PAYLOAD_WRAPPERS: tuple[str, ...] = (
    "data",
    "result",
    "Result",
    "Data",
    "payload",
    "response",
    "Response",
    "body",
)


def _databundle_payload_bases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Unwrap ``GetDatabundles`` JSON so list keys may sit under common wrappers."""
    bases: list[dict[str, Any]] = [payload]
    for w in _DATABUNDLE_PAYLOAD_WRAPPERS:
        inner = payload.get(w)
        if isinstance(inner, dict):
            bases.append(inner)
    return bases


def _normalize_databundle_container_rows(base: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse one dict level into databundle-shaped dict rows."""
    return _normalize_container_list(
        base,
        "Databundles",
        ("databundles", "Databundle", "databundle", "DataBundle", "data_bundles"),
    )


def _normalize_databundle_rows_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect databundle dict rows from top-level and wrapped dicts (``data``, …)."""
    for base in _databundle_payload_bases(payload):
        rows = _normalize_databundle_container_rows(base)
        if rows:
            return rows
    return []


def _parse_volumegroup_id(val: Any) -> int | None:
    """Resolve ``volumegroup`` to an integer id (scalar or nested object)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return int(val)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s)
        return None
    if isinstance(val, dict):
        for key in ("volumegroup", "Volumegroup", "id", "ID", "volume_group_id"):
            nested = val.get(key)
            if nested is not None and nested is not val:
                parsed = _parse_volumegroup_id(nested)
                if parsed is not None:
                    return parsed
    return None


def _parse_unix_timestamp_field(val: Any) -> int | None:
    """Parse epoch seconds for databundle/subscription date fields."""
    if isinstance(val, (int, float)):
        i = int(val)
        return i if i > 0 else None
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            i = int(s)
            return i if i > 0 else None
    return None


def _normalize_databundle_api_row(
    raw: dict[str, Any],
    *,
    source_subscription_id: str,
) -> dict[str, Any]:
    """Normalize one ``GetDatabundles`` row for linking and sensors."""
    db_id = (
        raw.get("databundle_id")
        or raw.get("DatabundleID")
        or raw.get("databundleId")
        or raw.get("id")
        or raw.get("ID")
    )
    db_id_s: str | None
    if db_id is None:
        db_id_s = None
    else:
        db_id_s = str(db_id).strip() or None

    pd_raw = (
        raw.get("product_description")
        or raw.get("ProductDescription")
        or raw.get("description")
        or raw.get("Description")
    )
    product_description = pd_raw.strip() if isinstance(pd_raw, str) else None

    st = raw.get("status")
    status_s = st.strip() if isinstance(st, str) and st.strip() else None

    start_i = _parse_unix_timestamp_field(
        raw.get("start_date") or raw.get("StartDate") or raw.get("start")
    )
    expire_i = _parse_unix_timestamp_field(
        raw.get("expire_date") or raw.get("ExpireDate") or raw.get("expire")
    )

    monthly_val: float | int | None = None
    mraw = raw.get("monthly")
    if mraw is None:
        mraw = raw.get("Monthly")
    if isinstance(mraw, bool):
        monthly_val = None
    elif isinstance(mraw, (int, float)):
        monthly_val = int(mraw) if float(mraw).is_integer() else float(mraw)
    elif isinstance(mraw, str):
        s = mraw.strip().replace(",", ".")
        if s:
            try:
                fv = float(s)
                monthly_val = int(fv) if fv.is_integer() else fv
            except ValueError:
                monthly_val = None

    vg_raw = raw.get("volumegroup") or raw.get("volume_group") or raw.get("VolumeGroup")
    volumegroup_id = _parse_volumegroup_id(vg_raw)

    row_sub_id: str | None = None
    for key in ("subscription_id", "SubscriptionID", "subscriptionId", "sub_id"):
        v = raw.get(key)
        if v is not None and str(v).strip():
            row_sub_id = str(v).strip()
            break

    out: dict[str, Any] = {
        "databundle_id": db_id_s,
        "product_description": product_description,
        "status": status_s,
        "start_date": start_i,
        "expire_date": expire_i,
        "monthly": monthly_val,
        "volumegroup_id": volumegroup_id,
        "subscription_id_from_row": row_sub_id,
        "source_subscription_id": source_subscription_id,
    }
    return out


def _finalize_linked_databundles(
    subscriptions: list[dict[str, Any]],
    *,
    max_rows: int,
) -> list[dict[str, Any]]:
    """Resolve ICCID / device target per databundle; dedupe and cap list length."""
    by_id: dict[str, dict[str, Any]] = {
        str(s["subscription_id"]): s for s in subscriptions if s.get("subscription_id")
    }

    flat: list[dict[str, Any]] = []
    for sub in subscriptions:
        pending = sub.pop("_databundle_rows", None)
        if not isinstance(pending, list):
            continue
        sid = str(sub.get("subscription_id", "")).strip()
        for row in pending:
            if not isinstance(row, dict):
                continue
            merged = dict(row)
            merged["_source_subscription_id"] = sid
            flat.append(merged)

    vg_to_sub_ids: dict[int, list[str]] = {}
    for sid, s in by_id.items():
        for vgid in s.get("volume_group_ids") or []:
            try:
                vi = int(vgid)
            except (TypeError, ValueError):
                continue
            vg_to_sub_ids.setdefault(vi, []).append(sid)

    for subs in vg_to_sub_ids.values():
        subs.sort()

    linked: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for row in flat:
        src_sid = str(row.get("_source_subscription_id") or "").strip()
        row_sub = row.get("subscription_id_from_row")
        row_sub_s = str(row_sub).strip() if row_sub is not None else ""
        vg_id = row.get("volumegroup_id")
        vg_int: int | None = int(vg_id) if isinstance(vg_id, int) else None

        candidates: list[str] = []
        if row_sub_s and row_sub_s in by_id:
            candidates.append(row_sub_s)
        if vg_int is not None:
            for sid in vg_to_sub_ids.get(vg_int, []):
                if sid not in candidates:
                    candidates.append(sid)

        chosen_sid: str | None = None
        if len(candidates) == 1:
            chosen_sid = candidates[0]
        elif len(candidates) > 1:
            if row_sub_s and row_sub_s in candidates:
                chosen_sid = row_sub_s
            elif src_sid and src_sid in candidates:
                chosen_sid = src_sid
            else:
                chosen_sid = candidates[0]
                _LOGGER.debug(
                    "Databundle volumegroup %s matches multiple subscriptions %s; "
                    "using first candidate (no row subscription_id, source=%s)",
                    vg_int,
                    candidates,
                    src_sid or "?",
                )
        elif src_sid and src_sid in by_id:
            chosen_sid = src_sid
        else:
            _LOGGER.debug(
                "Skipping databundle row (no subscription_id match, no volumegroup "
                "match, invalid source): keys=%s",
                sorted(str(k) for k in row.keys() if not str(k).startswith("_")),
            )
            continue

        if not chosen_sid or chosen_sid not in by_id:
            continue

        target = by_id[chosen_sid]
        iccid = target.get("iccid")
        iccid_n = _normalize_iccid(iccid) if isinstance(iccid, str) else None
        device_identifier = target.get("device_identifier")
        if not isinstance(device_identifier, str) or not device_identifier:
            device_identifier = iccid_n or f"sub_{chosen_sid}"

        db_id = row.get("databundle_id")
        dedupe_key = f"{db_id or row.get('product_description')}|{device_identifier}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        out_row: dict[str, Any] = {
            "databundle_id": db_id,
            "product_description": row.get("product_description"),
            "status": row.get("status"),
            "start_date": row.get("start_date"),
            "expire_date": row.get("expire_date"),
            "monthly": row.get("monthly"),
            "volumegroup_id": row.get("volumegroup_id"),
            "linked_subscription_id": chosen_sid,
            "iccid": iccid_n or (iccid if isinstance(iccid, str) else None),
            "device_identifier": device_identifier,
            "merged_onto_sim": bool(target.get("merged_onto_sim")),
            "source_subscription_id": row.get("source_subscription_id") or src_sid,
        }
        linked.append(out_row)

    if len(linked) > max_rows:
        total = len(linked)
        linked = linked[:max_rows]
        _LOGGER.debug(
            "Databundle rows capped: %s linked after dedupe, keeping first %s",
            total,
            max_rows,
        )

    return linked


def _normalize_iccid(val: Any) -> str | None:
    if isinstance(val, str) and val.strip():
        return val.strip().upper()
    return None


def _normalize_msisdn(val: Any) -> str | None:
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, (int, float)):
        return str(int(val))
    return None


def _first_str_from_dict(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_boolish(val: Any) -> bool | None:
    """Parse API booleans / 0-1 / common string tokens."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if val == 0:
            return False
        if val == 1:
            return True
        return None
    if isinstance(val, str):
        sl = val.strip().lower()
        if sl in ("true", "1", "yes", "y", "on", "ja"):
            return True
        if sl in ("false", "0", "no", "n", "off", "nee"):
            return False
    return None


def _parse_imei_lock_field(val: Any) -> str | bool | None:
    """IMEI lock flag or label; never return a string that looks like a full IMEI."""
    b = _parse_boolish(val)
    if b is not None:
        return b
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"\d{14,17}", s):
            return None  # looks like IMEI / IMSI digit run — omit from HA state
        if len(s) <= 32:
            return s
    return None


_SUBSCRIPTION_INFO_KEYS: tuple[str, ...] = (
    "SubscriptionInfo",
    "subscription_info",
    "subscriptionInfo",
)


def _subscription_info_payload_bases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Unwrap ``GetSubscriptionInfo`` JSON so ``SubscriptionInfo`` may sit under ``data``/``result``."""
    bases: list[dict[str, Any]] = [payload]
    for w in _SUBSCRIPTION_PAYLOAD_WRAPPERS:
        inner = payload.get(w)
        if isinstance(inner, dict):
            bases.append(inner)
    return bases


def _subscription_info_value_as_dict(val: Any) -> dict[str, Any] | None:
    """Treat ``SubscriptionInfo`` as a dict, or the first dict element if the API uses a list."""
    if isinstance(val, dict):
        return val
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                return item
    return None


def _dict_has_subscription_info_row_signals(d: dict[str, Any]) -> bool:
    """True when this dict carries detail fields typical of ``GetSubscriptionInfo`` (not only status)."""
    for k in (
        "SIMinfo",
        "sim_info",
        "simInfo",
        "Productinfo",
        "ProductInfo",
        "productinfo",
        "product_info",
        "subscription_id",
        "SubscriptionID",
        "subscriptionId",
        "UpgradeOptions",
        "upgrade_options",
        "upgradeOptions",
        "limit_data",
        "LimitData",
        "monthly",
        "Monthly",
        "imeilock",
        "imei_lock",
        "IMEILock",
    ):
        if k in d:
            return True
    return False


def _dict_is_only_api_success_shallow(d: dict[str, Any]) -> bool:
    """True when the dict has no merge-relevant keys besides API status/code (and empty wrappers)."""
    for k in d:
        ks = str(k)
        if ks in ("APIstatus", "APIcode", "status", "message", "Message"):
            continue
        if k in _SUBSCRIPTION_PAYLOAD_WRAPPERS:
            continue
        return False
    return True


def _subscription_info_dict(info_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the inner object for ``GetSubscriptionInfo`` (wrapped, list, or flat body)."""
    for base in _subscription_info_payload_bases(info_payload):
        for key in _SUBSCRIPTION_INFO_KEYS:
            inner = _subscription_info_value_as_dict(base.get(key))
            if inner is not None:
                return inner
        if _dict_has_subscription_info_row_signals(base) and not _dict_is_only_api_success_shallow(
            base
        ):
            return base
    return None


def _upgrade_options_dict(parent: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("UpgradeOptions", "upgrade_options", "upgradeOptions"):
        v = parent.get(key)
        if isinstance(v, dict):
            return v
    return None


def _product_info_dict(parent: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("Productinfo", "ProductInfo", "productinfo", "product_info"):
        v = parent.get(key)
        if isinstance(v, dict):
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    return item
    return None


def _parse_intish(val: Any) -> int | None:
    """Parse a whole number for ids (reject bool; allow int-like float)."""
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if val.is_integer():
            try:
                return int(val)
            except (ValueError, OverflowError):
                return None
        return None
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit() or (s.startswith("-") and len(s) > 1 and s[1:].isdigit()):
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _subscription_row_product_id(row: dict[str, Any]) -> int | None:
    """Portal product id from a raw ``GetSubscriptions``-shaped row or nested ``Productinfo``."""
    for key in ("product_id", "ProductID", "productId", "ProductId"):
        pid = _parse_intish(row.get(key))
        if pid is not None:
            return pid
    prod = _product_info_dict(row)
    if isinstance(prod, dict):
        for ik in ("id", "ID", "product_id", "ProductID"):
            pid = _parse_intish(prod.get(ik))
            if pid is not None:
                return pid
    return None


def _product_info_from_get_product_info_payload(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Product row from ``GetProductInfo`` success body."""
    for key in ("ProductInfo", "Productinfo", "product_info", "productInfo"):
        v = payload.get(key)
        if isinstance(v, dict):
            return v
    return None


def _merge_product_info_detail_into_subscription(
    base: dict[str, Any], detail: dict[str, Any]
) -> None:
    """Set ``product_info_detail`` and fill name/description/data only when missing.

    EUR amounts live only under ``product_info_detail`` so ``monthly`` (bool plan
    cadence from ``GetSubscriptionInfo``) is never conflated with product ``monthly`` price.
    """
    base["product_info_detail"] = dict(detail)

    if not base.get("product_name"):
        name = detail.get("name")
        if isinstance(name, str) and name.strip():
            base["product_name"] = name.strip()

    if not base.get("product_description"):
        for k in ("description", "long_description", "Description"):
            v = detail.get(k)
            if isinstance(v, str) and v.strip():
                base["product_description"] = v.strip()
                break

    if base.get("product_data_included_mb") is None:
        di = detail.get("data_included") or detail.get("DataIncluded")
        if isinstance(di, (int, float)):
            f_di = float(di)
            base["product_data_included_mb"] = f_di
            _, p_inc_b = _mb_pair_to_bytes(None, f_di)
            base["product_data_included_bytes"] = p_inc_b
            if base.get("data_allowance_bytes") is None:
                base["data_allowance_bytes"] = p_inc_b


def _sim_info_dict(parent: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("SIMinfo", "sim_info", "simInfo"):
        v = parent.get(key)
        if isinstance(v, dict):
            return v
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    return item
    return None


def _extract_dataproduct(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """Return a DataProduct-shaped dict from a GetDatabundles row (nested or flat)."""
    for key in ("DataProduct", "data_product", "DataProducts", "dataproduct"):
        v = bundle.get(key)
        if isinstance(v, dict):
            return v
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, dict):
                return first
    if isinstance(bundle.get("name"), str) and any(
        k in bundle for k in ("data_included", "DataIncluded", "monthly", "Monthly", "onetime", "Onetime")
    ):
        return bundle
    return None


def _dataproduct_summary(dp: dict[str, Any]) -> dict[str, Any]:
    """Normalize one DataProduct-like dict for coordinator rows (no secrets)."""
    name = _first_str_from_dict(dp, ("name", "Name", "description", "Description"))
    di_raw = dp.get("data_included")
    if di_raw is None:
        di_raw = dp.get("DataIncluded")
    di_mb: float | None = None
    if isinstance(di_raw, (int, float)):
        di_mb = float(di_raw)
    mraw = dp.get("monthly")
    if mraw is None:
        mraw = dp.get("Monthly")
    monthly = _parse_boolish(mraw)
    oraw = dp.get("onetime")
    if oraw is None:
        oraw = dp.get("Onetime")
    onetime = _parse_boolish(oraw)
    out: dict[str, Any] = {}
    if name:
        out["name"] = name
    if di_mb is not None:
        out["data_included_mb"] = di_mb
    if monthly is not None:
        out["monthly"] = monthly
    if onetime is not None:
        out["onetime"] = onetime
    return out


def _subscription_needs_get_info(sub: dict[str, Any]) -> bool:
    """Whether optional GetSubscriptionInfo may still add fields."""
    if sub.get("data_used_bytes") is None and sub.get("data_allowance_bytes") is None:
        return True
    if sub.get("monthly") is None:
        return True
    if sub.get("imei_lock") is None:
        return True
    return False


def _sim_iccid_set(sims: list[dict[str, Any]]) -> set[str]:
    s: set[str] = set()
    for sim in sims:
        ic = _normalize_iccid(sim.get("ICCID") or sim.get("iccid"))
        if ic:
            s.add(ic)
    return s


def _merge_subscription_info(
    base: dict[str, Any], info_payload: dict[str, Any]
) -> None:
    """Merge ``GetSubscriptionInfo`` into normalized row (never copies PIN/PUK)."""
    raw = _subscription_info_dict(info_payload)
    if not isinstance(raw, dict):
        return
    if base.get("status") is None and isinstance(raw.get("status"), str):
        base["status"] = raw["status"].strip()
    rs = _first_str_from_dict(
        raw,
        ("radius_status", "radiusStatus", "RadiusStatus", "RADIUS_status"),
    )
    if rs and base.get("radius_status") is None:
        base["radius_status"] = rs

    if base.get("ip") is None:
        ip_m = _first_str_from_dict(raw, ("ip", "IP", "Ip"))
        if ip_m:
            base["ip"] = ip_m
    if base.get("hostname") is None:
        hn_m = _first_str_from_dict(raw, ("hostname", "Hostname", "host", "Host"))
        if hn_m:
            base["hostname"] = hn_m

    if base.get("start_date") is None:
        sd = raw.get("start_date") if "start_date" in raw else raw.get("StartDate")
        if isinstance(sd, (int, float)):
            base["start_date"] = int(sd)
    if base.get("expire_date") is None:
        ed = raw.get("expire_date") if "expire_date" in raw else raw.get("ExpireDate")
        if isinstance(ed, (int, float)):
            base["expire_date"] = int(ed)

    mraw = raw.get("monthly")
    if mraw is None:
        mraw = raw.get("Monthly")
    mparsed = _parse_boolish(mraw)
    if mparsed is not None and base.get("monthly") is None:
        base["monthly"] = mparsed

    il_raw = None
    for key in ("imeilock", "imei_lock", "IMEILock", "ImeiLock", "IMEI_lock"):
        if key in raw:
            il_raw = raw.get(key)
            break
    if il_raw is not None and base.get("imei_lock") is None:
        parsed_il = _parse_imei_lock_field(il_raw)
        if parsed_il is not None:
            base["imei_lock"] = parsed_il

    lim: float | None = None
    for key in ("limit_data", "LimitData", "data_included", "DataIncluded"):
        v = raw.get(key)
        if isinstance(v, (int, float)):
            lim = float(v)
            break
    if lim is not None and base.get("data_allowance_bytes") is None:
        _, allow_b = _mb_pair_to_bytes(None, lim)
        base["data_allowance_bytes"] = allow_b
        base["limit_data_mb"] = lim

    prod = _product_info_dict(raw)
    if prod is None:
        uo = _upgrade_options_dict(raw)
        if isinstance(uo, dict):
            prod = _product_info_dict(uo)
    if isinstance(prod, dict):
        if not base.get("product_name"):
            desc = prod.get("description") or prod.get("product_description")
            if isinstance(desc, str) and desc.strip():
                base["product_name"] = desc.strip()
        if not base.get("product_description"):
            pd = prod.get("product_description") or prod.get("description")
            if isinstance(pd, str) and pd.strip():
                base["product_description"] = pd.strip()
        p_di = prod.get("data_included") or prod.get("DataIncluded")
        if isinstance(p_di, (int, float)):
            f_di = float(p_di)
            base["product_data_included_mb"] = f_di
            _, p_inc_b = _mb_pair_to_bytes(None, f_di)
            base["product_data_included_bytes"] = p_inc_b
            if base.get("data_allowance_bytes") is None:
                base["data_allowance_bytes"] = p_inc_b
        if base.get("product_id") is None:
            pvid = _parse_intish(
                prod.get("id")
                or prod.get("ID")
                or prod.get("product_id")
                or prod.get("ProductID")
            )
            if pvid is not None:
                base["product_id"] = pvid

    siminfo = _sim_info_dict(raw)
    if isinstance(siminfo, dict):
        # ICCID / description / MSISDN only — PIN/PUK must not enter coordinator state.
        ic = _normalize_iccid(siminfo.get("ICCID") or siminfo.get("iccid"))
        if ic and not base.get("iccid"):
            base["iccid"] = ic
            base["device_identifier"] = ic
        if not base.get("msisdn"):
            ms = _normalize_msisdn(siminfo.get("MSISDN") or siminfo.get("msisdn"))
            if ms:
                base["msisdn"] = ms
        sdesc = (
            siminfo.get("description")
            or siminfo.get("Description")
            or siminfo.get("sim_description")
            or siminfo.get("SimDescription")
        )
        if isinstance(sdesc, str) and sdesc.strip() and not base.get("sim_description"):
            base["sim_description"] = sdesc.strip()


_SIM_CREDENTIAL_KEYS_LOWER: frozenset[str] = frozenset(
    {
        "pin",
        "puk",
        "pin1",
        "puk1",
        "pin2",
        "puk2",
        "simpin",
        "simpuk",
    }
)


def _strip_sensitive_sim_fields_for_merge(d: dict[str, Any]) -> dict[str, Any]:
    """Drop PIN/PUK-style keys before merging SIM detail into coordinator rows."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(k, str):
            lk = k.strip().lower()
            if lk in _SIM_CREDENTIAL_KEYS_LOWER:
                continue
        out[k] = v
    return out


def _is_empty_for_sim_merge(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    return False


def _base_value_nonempty_for_sim_merge(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and v.strip():
        return True
    if isinstance(v, (list, dict)) and len(v) > 0:
        return True
    if isinstance(v, (int, float, bool)):
        return True
    return False


def _merge_sim_detail_into_row(base: dict[str, Any], detail_row: dict[str, Any]) -> None:
    """Merge ``GetSIM`` ``SIM`` object into a list row; never overwrite non-empty with empty."""
    cleaned = _strip_sensitive_sim_fields_for_merge(detail_row)
    for k, v in cleaned.items():
        if _is_empty_for_sim_merge(v) and _base_value_nonempty_for_sim_merge(base.get(k)):
            continue
        base[k] = v


def _sim_object_from_get_sim_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("SIM", "sim", "Sim"):
        block = payload.get(key)
        if isinstance(block, dict):
            return block
    return None


async def _enrich_sims_with_get_sim(
    client: OpenM2MClient,
    sims: list[dict[str, Any]],
    *,
    max_calls: int,
) -> None:
    """Call ``GetSIM`` for rows with ICCID but no description (capped per refresh)."""
    if max_calls <= 0:
        return
    candidates: list[tuple[str, dict[str, Any]]] = []
    for row in sims:
        if not isinstance(row, dict):
            continue
        ic = _normalize_iccid(row.get("ICCID") or row.get("iccid"))
        if not ic:
            continue
        if _sim_row_nonempty_description(row):
            continue
        candidates.append((ic, row))
    if not candidates:
        return
    for ic, row in candidates[:max_calls]:
        try:
            payload = await client.async_get_sim(ic)
        except OpenM2MError as err:
            _LOGGER.debug("GetSIM(%s) failed: %s", ic, err)
            continue
        detail = _sim_object_from_get_sim_payload(payload)
        if not isinstance(detail, dict):
            _LOGGER.debug(
                "GetSIM(%s) success but no SIM object; keys=%s",
                ic,
                sorted(str(k) for k in payload.keys())[:25],
            )
            continue
        _merge_sim_detail_into_row(row, detail)


def _cdr_row_total_usage_kb(row: dict[str, Any]) -> float | None:
    """Parse one CDR row's usage in kilobytes (portal key variants)."""
    for key in (
        "total_usage_kb",
        "TotalUsageKb",
        "total_usage_KB",
        "usage_kb",
        "UsageKb",
        "used_kb",
        "UsedKb",
        "Total_usage_kb",
    ):
        v = row.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().replace(",", ".")
            if s:
                try:
                    return float(s)
                except ValueError:
                    continue
    return None


def _cdr_rows_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``CDR`` may be one object or a list of CDR-shaped dicts."""
    raw = payload.get("CDR") or payload.get("cdr")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _usage_totals_from_get_usage_payload(
    payload: dict[str, Any],
    *,
    expect_year: int,
    expect_month: int,
) -> UsageTotalsMonth | None:
    """Sum ``total_usage_kb`` across CDR rows; ``year``/``month`` follow the requested period."""
    rows = _cdr_rows_from_payload(payload)
    if not rows:
        return None
    total = 0.0
    seen = False
    for row in rows:
        kb = _cdr_row_total_usage_kb(row)
        if kb is not None:
            total += kb
            seen = True
    if not seen:
        return None
    return {
        "year": expect_year,
        "month": expect_month,
        "total_usage_kb": total,
    }


async def _async_fetch_usage_totals_month(
    client: OpenM2MClient,
    year: int,
    month: int,
    subscription_ids: list[str],
) -> UsageTotalsMonth | None:
    """Account-level ``GetUsageTotals`` first; then up to ``MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES`` ids."""
    try:
        payload = await client.async_get_usage_totals(year=year, month=month)
        parsed = _usage_totals_from_get_usage_payload(
            payload, expect_year=year, expect_month=month
        )
        if parsed is not None:
            return parsed
    except OpenM2MError as err:
        _LOGGER.debug(
            "GetUsageTotals(%s-%02d) account-scoped failed: %s", year, month, err
        )

    total_kb = 0.0
    any_part = False
    for sid in subscription_ids[:MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES]:
        try:
            sid_i = int(str(sid).strip())
        except (TypeError, ValueError):
            continue
        try:
            payload = await client.async_get_usage_totals(
                year=year, month=month, subscription_id=sid_i
            )
        except OpenM2MError as err:
            _LOGGER.debug(
                "GetUsageTotals(%s-%02d, subscription_id=%s) failed: %s",
                year,
                month,
                sid,
                err,
            )
            continue
        part = _usage_totals_from_get_usage_payload(
            payload, expect_year=year, expect_month=month
        )
        if part is not None:
            total_kb += float(part["total_usage_kb"])
            any_part = True
    if any_part:
        return {
            "year": year,
            "month": month,
            "total_usage_kb": total_kb,
        }
    return None


async def _gather_limited(
    coros: list[Any], *, limit: int
) -> list[Any]:
    """Run awaitables with a bounded concurrency."""
    sem = asyncio.Semaphore(limit)

    async def _run(c: Any) -> Any:
        async with sem:
            return await c

    return await asyncio.gather(*[_run(c) for c in coros])


class OpenM2MCoordinator(DataUpdateCoordinator[OpenM2MCoordinatorData]):
    """Poll account info, SIM list, and subscription/bundle data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OpenM2MClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
            config_entry=entry,
        )
        self.client = client

    async def _async_update_data(self) -> OpenM2MCoordinatorData:
        try:
            account = await self.client.async_get_account_info()
            sims_raw = await self.client.async_get_sims()
        except OpenM2MError as err:
            raise UpdateFailed(f"Open-M2M request failed: {err}") from err

        if not isinstance(account, dict):
            _LOGGER.warning("Account info was not a dict; coercing to empty")
            account = {}
        if not isinstance(sims_raw, dict):
            _LOGGER.warning("SIM list response was not a dict; coercing to empty")
            sims_raw = {}

        balance, balance_raw = _extract_balance(account)
        sims = _normalize_sims(sims_raw)
        await _enrich_sims_with_get_sim(
            self.client,
            sims,
            max_calls=MAX_SIM_DETAIL_ENRICHMENT_PER_REFRESH,
        )
        sim_iccids = _sim_iccid_set(sims)

        subscriptions: list[dict[str, Any]] = []
        aggregate_remaining: int | None = None

        try:
            sub_payload = await self.client.async_get_subscriptions()
        except OpenM2MError as err:
            _LOGGER.warning("GetSubscriptions failed: %s", err)
            sub_payload = {}

        sub_rows = _normalize_subscription_rows(sub_payload)
        if not sub_rows and isinstance(sub_payload, dict) and sub_payload:
            _LOGGER.debug(
                "GetSubscriptions succeeded but parsed 0 subscription rows; "
                "top-level keys: %s",
                sorted(str(k) for k in sub_payload.keys()),
            )

        async def enrich_one(row: dict[str, Any]) -> dict[str, Any]:
            _ensure_subscription_id(row)
            sid = row.get("subscription_id")
            sid_s = str(sid).strip() if sid is not None else ""
            if not sid_s:
                return {}
            iccid = _normalize_iccid(row.get("ICCID") or row.get("iccid"))
            msisdn = _normalize_msisdn(row.get("MSISDN") or row.get("msisdn"))
            imsi_raw = row.get("IMSI") or row.get("imsi")
            imsi_s = imsi_raw.strip() if isinstance(imsi_raw, str) else None
            product_name: str | None = None
            product_description_s: str | None = None
            pd = row.get("product_description") or row.get("ProductDescription")
            if isinstance(pd, str) and pd.strip():
                product_name = pd.strip()
                product_description_s = pd.strip()
            status = row.get("status")
            status_s = status.strip() if isinstance(status, str) else None
            ar = row.get("auto_renewal") or row.get("AutoRenewal")
            auto_renew: bool | None = ar if isinstance(ar, bool) else _parse_boolish(ar)
            start_date = row.get("start_date") or row.get("StartDate")
            expire_date = row.get("expire_date") or row.get("ExpireDate")
            start_i = int(start_date) if isinstance(start_date, (int, float)) else None
            expire_i = int(expire_date) if isinstance(expire_date, (int, float)) else None
            sim_desc = row.get("sim_description") or row.get("SimDescription")
            sim_desc_s = sim_desc.strip() if isinstance(sim_desc, str) else None
            ip_s = _first_str_from_dict(row, ("ip", "IP", "Ip"))
            hostname_s = _first_str_from_dict(
                row, ("hostname", "Hostname", "host", "Host")
            )
            radius_s = _first_str_from_dict(
                row,
                ("radius_status", "radiusStatus", "RadiusStatus", "RADIUS_status"),
            )
            mraw_row = row.get("monthly")
            if mraw_row is None:
                mraw_row = row.get("Monthly")
            monthly_row = _parse_boolish(mraw_row)
            il_raw_row = None
            for key in ("imeilock", "imei_lock", "IMEILock", "ImeiLock", "IMEI_lock"):
                if key in row:
                    il_raw_row = row.get(key)
                    break
            imei_lock_row = _parse_imei_lock_field(il_raw_row) if il_raw_row is not None else None

            bundle_names: list[str] = []
            dataproduct_summaries: list[dict[str, Any]] = []
            used_mb_total = 0.0
            vol_mb_total = 0.0
            seen_vg: set[int] = set()
            has_vg_numbers = False

            db_raw: dict[str, Any] = {}
            vg_raw_sub: dict[str, Any] = {}
            g_res = await asyncio.gather(
                self.client.async_get_databundles(sid_s),
                self.client.async_get_volume_groups(sid_s),
                return_exceptions=True,
            )
            for idx, label in enumerate(("GetDatabundles", "GetVolumeGroups")):
                item = g_res[idx]
                if isinstance(item, OpenM2MError):
                    _LOGGER.debug("%s(%s) failed: %s", label, sid_s, item)
                elif isinstance(item, Exception):
                    _LOGGER.warning("%s(%s) unexpected: %s", label, sid_s, item)
                elif isinstance(item, dict):
                    if idx == 0:
                        db_raw = item
                    else:
                        vg_raw_sub = item

            vg_map_local = _volume_groups_map(vg_raw_sub)
            volume_group_ids = sorted(int(x) for x in vg_map_local.keys())

            bundles = _normalize_databundle_rows_from_payload(db_raw)
            databundle_rows_for_link: list[dict[str, Any]] = []
            bundle_summaries: list[dict[str, Any]] = []
            for b in bundles:
                bname = b.get("product_description")
                if isinstance(bname, str) and bname.strip():
                    bundle_names.append(bname.strip())
                vgid_raw = b.get("volumegroup") or b.get("volume_group") or b.get("VolumeGroup")
                vgid = _parse_volumegroup_id(vgid_raw)
                vg_row: dict[str, Any] | None = (
                    vg_map_local.get(vgid) if vgid is not None else None
                )
                used_mb: float | None = None
                vol_mb: float | None = None
                if vg_row is not None and vgid is not None and vgid not in seen_vg:
                    seen_vg.add(vgid)
                    u_raw = vg_row.get("used")
                    v_raw = vg_row.get("volume")
                    if isinstance(u_raw, (int, float)):
                        used_mb_total += float(u_raw)
                        used_mb = float(u_raw)
                        has_vg_numbers = True
                    if isinstance(v_raw, (int, float)):
                        vol_mb_total += float(v_raw)
                        vol_mb = float(v_raw)
                        has_vg_numbers = True
                dp = _extract_dataproduct(b)
                dp_sum: dict[str, Any] | None = None
                if dp is not None:
                    dp_sum = _dataproduct_summary(dp)
                    if dp_sum:
                        dataproduct_summaries.append(dp_sum)
                        if isinstance(dp_sum.get("name"), str) and dp_sum["name"] not in bundle_names:
                            bundle_names.append(dp_sum["name"])

                bundle_summaries.append(
                    {
                        "product_description": bname if isinstance(bname, str) else None,
                        "status": b.get("status"),
                        "volumegroup_id": vgid,
                        "used_mb": used_mb,
                        "volume_mb": vol_mb,
                        "dataproduct": dp_sum,
                    }
                )
                databundle_rows_for_link.append(
                    _normalize_databundle_api_row(b, source_subscription_id=sid_s)
                )

            used_b: int | None = None
            allow_b: int | None = None
            if has_vg_numbers:
                used_b, allow_b = _mb_pair_to_bytes(used_mb_total, vol_mb_total)

            dp_allow_b: int | None = None
            dp_di_mb: float | None = None
            if dataproduct_summaries:
                first_mb = dataproduct_summaries[0].get("data_included_mb")
                if isinstance(first_mb, (int, float)):
                    dp_di_mb = float(first_mb)
                    _, dp_allow_b = _mb_pair_to_bytes(None, dp_di_mb)

            norm: dict[str, Any] = {
                "subscription_id": sid_s,
                "iccid": iccid,
                "msisdn": msisdn,
                "imsi": imsi_s,
                "status": status_s,
                "product_name": product_name,
                "product_description": product_description_s,
                "auto_renewal": auto_renew,
                "start_date": start_i,
                "expire_date": expire_i,
                "sim_description": sim_desc_s,
                "ip": ip_s,
                "hostname": hostname_s,
                "radius_status": radius_s,
                "monthly": monthly_row,
                "imei_lock": imei_lock_row,
                "data_used_bytes": used_b,
                "data_allowance_bytes": allow_b,
                "bundle_names": bundle_names,
                "bundle_primary_name": bundle_names[0] if bundle_names else None,
                "bundle_summaries": bundle_summaries,
                "dataproduct_summaries": dataproduct_summaries,
                "dataproduct_data_included_mb": dp_di_mb,
                "limit_data_mb": None,
                "product_data_included_mb": None,
                "volume_group_ids": volume_group_ids,
                "_databundle_rows": databundle_rows_for_link,
            }
            if norm["data_allowance_bytes"] is None and dp_allow_b is not None:
                norm["data_allowance_bytes"] = dp_allow_b
            if dp_di_mb is not None:
                _, dpb = _mb_pair_to_bytes(None, dp_di_mb)
                norm["dataproduct_data_included_bytes"] = dpb
            else:
                norm["dataproduct_data_included_bytes"] = None
            row_pid = _subscription_row_product_id(row)
            if row_pid is not None:
                norm["product_id"] = row_pid
            return norm

        enriched = await _gather_limited(
            [enrich_one(r) for r in sub_rows if isinstance(r, dict)],
            limit=SUBSCRIPTION_FETCH_CONCURRENCY,
        )
        subscriptions = [x for x in enriched if x]

        msisdn_to_iccid: dict[str, str] = {}
        imsi_to_iccid: dict[str, str] = {}
        for sim in sims:
            ic = _normalize_iccid(sim.get("ICCID") or sim.get("iccid"))
            if not ic:
                continue
            ms = _normalize_msisdn(sim.get("MSISDN") or sim.get("msisdn"))
            if ms:
                msisdn_to_iccid[ms] = ic
            for ik in ("IMSI", "imsi"):
                imsi_val = sim.get(ik)
                if isinstance(imsi_val, str) and imsi_val.strip():
                    imsi_to_iccid[imsi_val.strip()] = ic

        for sub in subscriptions:
            if not sub.get("iccid"):
                ms = sub.get("msisdn")
                if isinstance(ms, str) and ms in msisdn_to_iccid:
                    sub["iccid"] = msisdn_to_iccid[ms]
            imsi_sub = sub.get("imsi")
            if not sub.get("iccid") and isinstance(imsi_sub, str) and imsi_sub.strip() in imsi_to_iccid:
                sub["iccid"] = imsi_to_iccid[imsi_sub.strip()]

        # Optional ``GetSubscriptionInfo`` (capped): usage/allowance gaps first,
        # then rows still missing monthly / IMEI lock / RADIUS hints.
        def _info_sort_key(s: dict[str, Any]) -> tuple[int, str]:
            missing_bytes = (
                s.get("data_used_bytes") is None
                and s.get("data_allowance_bytes") is None
            )
            return (0 if missing_bytes else 1, str(s.get("subscription_id", "")))

        info_candidates = sorted(
            (s for s in subscriptions if _subscription_needs_get_info(s)),
            key=_info_sort_key,
        )[:MAX_SUBSCRIPTION_INFO_CALLS]

        async def fetch_info(sub: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            sid = sub["subscription_id"]
            try:
                payload = await self.client.async_get_subscription_info(sid)
            except OpenM2MError as err:
                _LOGGER.debug("GetSubscriptionInfo(%s) failed: %s", sid, err)
                return sid, None
            return sid, payload

        info_results = await _gather_limited(
            [fetch_info(s) for s in info_candidates],
            limit=SUBSCRIPTION_FETCH_CONCURRENCY,
        )
        by_id = {s["subscription_id"]: s for s in subscriptions}
        for sid, payload in info_results:
            if not payload or sid not in by_id:
                continue
            _merge_subscription_info(by_id[sid], payload)

        # Optional ``GetProductInfo``: up to ``MAX_PRODUCT_INFO_FETCHES`` unique
        # ``product_id`` values (after list + subscription-info merge).
        pid_order: list[int] = []
        seen_pid: set[int] = set()
        for sub in sorted(subscriptions, key=lambda s: str(s.get("subscription_id", ""))):
            pid = _parse_intish(sub.get("product_id"))
            if pid is None or pid in seen_pid:
                continue
            seen_pid.add(pid)
            pid_order.append(pid)
        pid_order = pid_order[:MAX_PRODUCT_INFO_FETCHES]

        async def fetch_product(pid: int) -> tuple[int, dict[str, Any] | None]:
            try:
                payload = await self.client.async_get_product_info(pid)
            except OpenM2MError as err:
                _LOGGER.debug("GetProductInfo(%s) failed: %s", pid, err)
                return pid, None
            return pid, payload

        product_results = await _gather_limited(
            [fetch_product(p) for p in pid_order],
            limit=SUBSCRIPTION_FETCH_CONCURRENCY,
        )
        detail_by_pid: dict[int, dict[str, Any]] = {}
        for pid, payload in product_results:
            if not isinstance(payload, dict):
                continue
            detail = _product_info_from_get_product_info_payload(payload)
            if isinstance(detail, dict):
                detail_by_pid[pid] = detail

        for sub in subscriptions:
            pid = _parse_intish(sub.get("product_id"))
            if pid is None or pid not in detail_by_pid:
                continue
            _merge_product_info_detail_into_subscription(sub, detail_by_pid[pid])

        for sub in subscriptions:
            ic = sub.get("iccid")
            if isinstance(ic, str) and ic:
                sub["device_identifier"] = ic
                sub["merged_onto_sim"] = ic in sim_iccids
            else:
                sub["device_identifier"] = f"sub_{sub['subscription_id']}"
                sub["merged_onto_sim"] = False

        remain_sum = 0
        remain_parts = 0
        for sub in subscriptions:
            u = sub.get("data_used_bytes")
            a = sub.get("data_allowance_bytes")
            if isinstance(u, int) and isinstance(a, int) and a >= u:
                remain_sum += a - u
                remain_parts += 1
        if remain_parts:
            aggregate_remaining = remain_sum
        else:
            aggregate_remaining = None

        databundles = _finalize_linked_databundles(
            subscriptions,
            max_rows=MAX_DATABUNDLES,
        )

        now_utc = datetime.now(timezone.utc)
        cur_y, cur_m = now_utc.year, now_utc.month
        if cur_m == 1:
            prev_y, prev_m = cur_y - 1, 12
        else:
            prev_y, prev_m = cur_y, cur_m - 1

        sub_ids_for_usage = [
            str(s["subscription_id"])
            for s in subscriptions
            if isinstance(s, dict) and s.get("subscription_id")
        ]

        usage_current = await _async_fetch_usage_totals_month(
            self.client, cur_y, cur_m, sub_ids_for_usage
        )
        usage_previous = await _async_fetch_usage_totals_month(
            self.client, prev_y, prev_m, sub_ids_for_usage
        )

        data: OpenM2MCoordinatorData = {
            "account": account,
            "account_balance": balance,
            "sims": sims,
            "subscriptions": subscriptions,
            "subscriptions_aggregate_remaining_bytes": aggregate_remaining,
            "databundles": databundles,
            "usage_totals_current": usage_current,
            "usage_totals_previous": usage_previous,
        }
        if balance_raw is not None:
            data["account_balance_raw"] = balance_raw
        return data
