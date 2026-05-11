"""DataUpdateCoordinator for Open-M2M account + SIM list + subscriptions."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, NotRequired, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenM2MClient, OpenM2MError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Concurrency for per-subscription GETs (OpenAPI requires ``subscription_id``).
SUBSCRIPTION_FETCH_CONCURRENCY = 3
# Cap optional ``GetSubscriptionInfo`` calls per refresh to avoid slowdowns.
MAX_SUBSCRIPTION_INFO_CALLS = 10


class OpenM2MCoordinatorData(TypedDict):
    """Structured data exposed on ``coordinator.data``."""

    account: dict[str, Any]
    account_balance: float | None
    sims: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]
    subscriptions_aggregate_remaining_bytes: int | None
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


def _normalize_sims(sims_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn ``GetSIMs`` JSON into a list of SIM dicts.

    The published OpenAPI models ``SIMs`` as an object; live responses may use
    a list or a single SIM-shaped dict. Parse defensively and log surprises.
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
        if any(k in raw for k in ("ICCID", "iccid", "subscription_id")):
            return [raw]
        values = [v for v in raw.values() if isinstance(v, dict)]
        if values:
            return values
        _LOGGER.warning(
            "GetSIMs returned SIMs object without recognizable SIM entries; keys=%s",
            list(raw.keys())[:20],
        )
        return []

    _LOGGER.warning(
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
)


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
        if any(m in raw for m in _SUBSCRIPTION_ROW_MARKERS):
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
    """Collect subscription rows from top-level and common wrapper dicts."""
    for base in _subscription_payload_bases(payload):
        rows = _normalize_subscription_container(base)
        if rows:
            return rows
    return []


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
    """Merge ``GetSubscriptionInfo`` ``SubscriptionInfo`` into normalized row."""
    raw = info_payload.get("SubscriptionInfo")
    if not isinstance(raw, dict):
        return
    if base.get("status") is None and isinstance(raw.get("status"), str):
        base["status"] = raw["status"].strip()
    lim = raw.get("limit_data")
    if isinstance(lim, (int, float)) and base.get("data_allowance_bytes") is None:
        _, allow_b = _mb_pair_to_bytes(None, float(lim))
        base["data_allowance_bytes"] = allow_b
        base["limit_data_mb"] = float(lim)
    prod = raw.get("Productinfo")
    if isinstance(prod, dict) and not base.get("product_name"):
        desc = prod.get("description") or prod.get("product_description")
        if isinstance(desc, str) and desc.strip():
            base["product_name"] = desc.strip()
    siminfo = raw.get("SIMinfo")
    if isinstance(siminfo, dict):
        ic = _normalize_iccid(siminfo.get("ICCID") or siminfo.get("iccid"))
        if ic and not base.get("iccid"):
            base["iccid"] = ic
            base["device_identifier"] = ic


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
            pd = row.get("product_description")
            if isinstance(pd, str) and pd.strip():
                product_name = pd.strip()
            status = row.get("status")
            status_s = status.strip() if isinstance(status, str) else None
            ar = row.get("auto_renewal")
            auto_renew: bool | None = ar if isinstance(ar, bool) else None
            start_date = row.get("start_date")
            expire_date = row.get("expire_date")
            start_i = int(start_date) if isinstance(start_date, (int, float)) else None
            expire_i = int(expire_date) if isinstance(expire_date, (int, float)) else None
            sim_desc = row.get("sim_description")
            sim_desc_s = sim_desc.strip() if isinstance(sim_desc, str) else None

            bundle_names: list[str] = []
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

            bundles = _normalize_container_list(
                db_raw, "Databundles", ("databundles", "Databundle")
            )
            bundle_summaries: list[dict[str, Any]] = []
            for b in bundles:
                bname = b.get("product_description")
                if isinstance(bname, str) and bname.strip():
                    bundle_names.append(bname.strip())
                vgid_raw = b.get("volumegroup") or b.get("volume_group")
                vgid: int | None = None
                if vgid_raw is not None:
                    try:
                        vgid = int(vgid_raw)
                    except (TypeError, ValueError):
                        vgid = None
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
                bundle_summaries.append(
                    {
                        "product_description": bname if isinstance(bname, str) else None,
                        "status": b.get("status"),
                        "volumegroup_id": vgid,
                        "used_mb": used_mb,
                        "volume_mb": vol_mb,
                    }
                )

            used_b: int | None = None
            allow_b: int | None = None
            if has_vg_numbers:
                used_b, allow_b = _mb_pair_to_bytes(used_mb_total, vol_mb_total)

            norm: dict[str, Any] = {
                "subscription_id": sid_s,
                "iccid": iccid,
                "msisdn": msisdn,
                "imsi": imsi_s,
                "status": status_s,
                "product_name": product_name,
                "auto_renewal": auto_renew,
                "start_date": start_i,
                "expire_date": expire_i,
                "sim_description": sim_desc_s,
                "data_used_bytes": used_b,
                "data_allowance_bytes": allow_b,
                "bundle_names": bundle_names,
                "bundle_primary_name": bundle_names[0] if bundle_names else None,
                "bundle_summaries": bundle_summaries,
                "limit_data_mb": None,
            }
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

        # Optional ``GetSubscriptionInfo`` when usage/allowance still missing.
        info_candidates = [
            s
            for s in subscriptions
            if s.get("data_used_bytes") is None and s.get("data_allowance_bytes") is None
        ][:MAX_SUBSCRIPTION_INFO_CALLS]

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

        data: OpenM2MCoordinatorData = {
            "account": account,
            "account_balance": balance,
            "sims": sims,
            "subscriptions": subscriptions,
            "subscriptions_aggregate_remaining_bytes": aggregate_remaining,
        }
        if balance_raw is not None:
            data["account_balance_raw"] = balance_raw
        return data
