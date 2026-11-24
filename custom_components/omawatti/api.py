"""OmaWatti (Lahti Energia) async API client and pure data-processing helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
import re
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import (
    API_BASE_URL,
    ENDPOINT_AUTHENTICATE,
    ENDPOINT_CONTRACTS,
    REQUEST_TIMEOUT_SECONDS,
    TOKEN_TTL_SECONDS,
    USAGE_TYPE_ELECTRICITY,
    USAGE_TYPE_HEATING,
)

if TYPE_CHECKING:
    import aiohttp

try:
    HELSINKI_TZ: tzinfo = ZoneInfo("Europe/Helsinki")
except ZoneInfoNotFoundError:  # Fallback on minimal Windows Python without tzdata
    HELSINKI_TZ = timezone(timedelta(hours=2))

_FI_TRANSLATION = str.maketrans(
    {
        "ä": "a",
        "ö": "o",
        "å": "a",
        "Ä": "a",
        "Ö": "o",
        "Å": "a",
    }
)


class OmaWattiError(Exception):
    """Base exception for OmaWatti API errors."""


class OmaWattiAuthError(OmaWattiError):
    """Authentication or authorization error."""


class OmaWattiApiError(OmaWattiError):
    """Transient network, HTTP, or API payload error."""


@dataclass(frozen=True, slots=True)
class ContractInfo:
    """Normalized active OmaWatti metering point contract."""

    metering_point: str
    usage_place_type: str  # "ELE" or "HEA"
    slug: str
    display_name: str
    ui_label: str
    street_name: str
    city: str
    product: str
    customer_number: str

    def to_config_dict(self) -> dict[str, str]:
        """Serialize contract for Home Assistant config entry storage."""
        return {
            "metering_point": self.metering_point,
            "usage_place_type": self.usage_place_type,
            "slug": self.slug,
            "display_name": self.display_name,
            "street_name": self.street_name,
            "city": self.city,
            "product": self.product,
        }


@dataclass(frozen=True, slots=True)
class HourlyPoint:
    """Single UTC hour-aligned measurement point."""

    start: datetime  # Always UTC, minute=0, second=0, microsecond=0
    value: float


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """Summary values computed from recent hourly points for dashboard tiles."""

    latest_day_date: str | None = None
    latest_day_total: float | None = None
    latest_day_avg: float | None = None
    latest_day_hours: int = 0
    last_7d_total: float | None = None
    latest_value: float | None = None
    latest_timestamp: str | None = None


def slugify_fi(text: str) -> str:
    """Convert Finnish text into a safe Home Assistant statistic_id slug."""
    ascii_text = text.translate(_FI_TRANSLATION).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")
    return slug or "meter"


def is_active_contract(raw: dict[str, Any], *, today: date | None = None) -> bool:
    """Return True if an OmaWatti contract is currently active."""
    valid_to = str(raw.get("validTo") or "").strip()
    if not valid_to or valid_to.lower() == "toistaiseksi":
        return True

    ref_today = today or date.today()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(valid_to, fmt).date() >= ref_today
        except ValueError:
            continue
    return False


def parse_active_contracts(
    raw_contracts: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[ContractInfo]:
    """Filter, deduplicate, and normalize active ELE/HEA contracts."""
    seen: set[tuple[str, str]] = set()
    active_raw: list[dict[str, Any]] = []

    for item in raw_contracts:
        usage_type = str(item.get("usagePlaceType") or "").upper()
        if usage_type not in (USAGE_TYPE_ELECTRICITY, USAGE_TYPE_HEATING):
            continue
        if not is_active_contract(item, today=today):
            continue

        mp_obj = item.get("meteringPoint") or {}
        mp_num = str(mp_obj.get("Number") or "").strip()
        if not mp_num:
            continue

        key = (usage_type, mp_num)
        if key in seen:
            continue
        seen.add(key)
        active_raw.append(item)

    # Sort deterministically so duplicate slug suffixes (_2, _3) never shift.
    active_raw.sort(
        key=lambda c: (
            str(c.get("usagePlaceType") or ""),
            str((c.get("meteringPoint") or {}).get("StreetName") or "").lower(),
            str((c.get("meteringPoint") or {}).get("Number") or ""),
        )
    )

    used_slugs: dict[tuple[str, str], int] = defaultdict(int)
    results: list[ContractInfo] = []

    for item in active_raw:
        usage_type = str(item["usagePlaceType"]).upper()
        mp_obj = item.get("meteringPoint") or {}
        mp_num = str(mp_obj["Number"]).strip()
        street = str(mp_obj.get("StreetName") or "").strip()
        house = str(mp_obj.get("HouseNumber") or "").strip()
        city_raw = str(mp_obj.get("City") or "").strip()
        city = city_raw.title() if city_raw.isupper() else city_raw
        product = str(
            item.get("productDescription") or item.get("typeDesc") or ""
        ).strip()
        customer_number = str(item.get("customerNumber") or "").strip()

        base_name = street or city or f"Kohde {mp_num[-4:]}"
        base_slug = slugify_fi(base_name)
        used_slugs[(usage_type, base_slug)] += 1
        idx = used_slugs[(usage_type, base_slug)]
        slug = base_slug if idx == 1 else f"{base_slug}_{idx}"
        display_name = base_name if idx == 1 else f"{base_name} {idx}"

        type_label = "Sähkö" if usage_type == USAGE_TYPE_ELECTRICITY else "Kaukolämpö"
        addr_parts = [p for p in (f"{street} {house}".strip(), city) if p]
        addr_str = ", ".join(addr_parts) if addr_parts else display_name
        last4 = mp_num[-4:] if len(mp_num) >= 4 else mp_num
        product_part = f" [{product}]" if product else ""
        ui_label = f"{type_label} – {addr_str} (…{last4}){product_part}"

        results.append(
            ContractInfo(
                metering_point=mp_num,
                usage_place_type=usage_type,
                slug=slug,
                display_name=display_name,
                ui_label=ui_label,
                street_name=street,
                city=city,
                product=product,
                customer_number=customer_number,
            )
        )

    return results


def _parse_utc_hour(time_str: str) -> datetime | None:
    """Parse an OmaWatti ISO timestamp and floor it to its UTC hour boundary."""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HELSINKI_TZ)
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _parse_numeric_value(raw_val: Any) -> float | None:
    """Parse reading value, returning None for null/empty/non-numeric entries."""
    if raw_val is None or raw_val == "":
        return None
    if isinstance(raw_val, bool):
        return None
    try:
        return float(raw_val)
    except (TypeError, ValueError):
        return None


def aggregate_quarterly_to_hourly(
    readings: list[dict[str, Any]],
    *,
    multiplier: float = 1.0,
) -> list[HourlyPoint]:
    """Sum 15-minute electricity readings into UTC-aligned hourly totals.

    Skips unmeasured future slots (`value: null`). Because timestamps include
    explicit timezone offsets (+02:00 / +03:00), DST transitions produce
    distinct UTC hours with zero overlap.
    """
    buckets: dict[datetime, float] = defaultdict(float)

    for row in readings:
        val = _parse_numeric_value(row.get("value"))
        if val is None:
            continue
        hour_utc = _parse_utc_hour(str(row.get("time") or ""))
        if hour_utc is None:
            continue
        buckets[hour_utc] += val * multiplier

    return [
        HourlyPoint(start=hour, value=round(total, 6))
        for hour, total in sorted(buckets.items())
    ]


def parse_hourly_readings(
    readings: list[dict[str, Any]],
    *,
    multiplier: float = 1.0,
) -> list[HourlyPoint]:
    """Parse hourly OmaWatti readings into UTC-aligned points, skipping nulls."""
    points_by_hour: dict[datetime, float] = {}

    for row in readings:
        val = _parse_numeric_value(row.get("value"))
        if val is None:
            continue
        hour_utc = _parse_utc_hour(str(row.get("time") or ""))
        if hour_utc is None:
            continue
        points_by_hour[hour_utc] = round(val * multiplier, 6)

    return [
        HourlyPoint(start=hour, value=val)
        for hour, val in sorted(points_by_hour.items())
    ]


def compute_delta_t(
    water_in: list[HourlyPoint],
    water_out: list[HourlyPoint],
) -> list[HourlyPoint]:
    """Compute hourly district heating cooling delta (T_in - T_out)."""
    out_by_hour = {pt.start: pt.value for pt in water_out}
    result: list[HourlyPoint] = []
    for pt in water_in:
        t_out = out_by_hour.get(pt.start)
        if t_out is not None:
            result.append(HourlyPoint(start=pt.start, value=round(pt.value - t_out, 2)))
    return result


def build_cumulative_statistics(
    points: list[HourlyPoint],
    initial_sum: float = 0.0,
) -> list[dict[str, Any]]:
    """Build HA external StatisticData dicts with monotonic cumulative sum."""
    running_sum = float(initial_sum)
    stats: list[dict[str, Any]] = []
    for pt in points:
        running_sum = round(running_sum + pt.value, 6)
        stats.append(
            {
                "start": pt.start,
                "state": round(pt.value, 6),
                "sum": running_sum,
            }
        )
    return stats


def build_mean_statistics(points: list[HourlyPoint]) -> list[dict[str, Any]]:
    """Build HA external StatisticData dicts for mean measurements (e.g. °C)."""
    return [
        {
            "start": pt.start,
            "mean": round(pt.value, 2),
            "min": round(pt.value, 2),
            "max": round(pt.value, 2),
        }
        for pt in points
    ]


def iter_year_chunks(start_date: date, end_date: date) -> list[tuple[date, date]]:
    """Split a date range into inclusive calendar-year chunks."""
    if start_date > end_date:
        return []
    chunks: list[tuple[date, date]] = []
    cur = start_date
    while cur <= end_date:
        year_end = date(cur.year, 12, 31)
        chunk_end = min(year_end, end_date)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def compute_metric_summary(
    points: list[HourlyPoint],
    *,
    local_tz: tzinfo = HELSINKI_TZ,
    min_complete_hours: int = 20,
) -> MetricSummary:
    """Compute latest complete day, 7-day rolling total, and newest reading."""
    if not points:
        return MetricSummary()

    by_local_day: dict[date, list[float]] = defaultdict(list)
    for pt in points:
        local_day = pt.start.astimezone(local_tz).date()
        by_local_day[local_day].append(pt.value)

    sorted_days = sorted(by_local_day.keys())
    complete_days = [d for d in sorted_days if len(by_local_day[d]) >= min_complete_hours]
    target_day = complete_days[-1] if complete_days else sorted_days[-1]
    target_vals = by_local_day[target_day]

    day_total = round(sum(target_vals), 3)
    day_avg = round(sum(target_vals) / len(target_vals), 2)

    window_start = target_day - timedelta(days=6)
    last_7d_vals = [
        v
        for d, vals in by_local_day.items()
        if window_start <= d <= target_day
        for v in vals
    ]
    last_7d_total = round(sum(last_7d_vals), 3) if last_7d_vals else None

    newest = points[-1]
    return MetricSummary(
        latest_day_date=target_day.isoformat(),
        latest_day_total=day_total,
        latest_day_avg=day_avg,
        latest_day_hours=len(target_vals),
        last_7d_total=last_7d_total,
        latest_value=round(newest.value, 3),
        latest_timestamp=newest.start.astimezone(local_tz).isoformat(),
    )


class OmaWattiClient:
    """Async REST client for Lahti Energia OmaWatti backend API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_obtained_at: float = 0.0
        self._customer_number: str | None = None
        self._auth_lock = asyncio.Lock()

    @property
    def customer_number(self) -> str | None:
        """Return customerNumber returned by the last authentication."""
        return self._customer_number

    def _token_is_valid(self) -> bool:
        return (
            self._token is not None
            and self._customer_number is not None
            and (time.monotonic() - self._token_obtained_at) < TOKEN_TTL_SECONDS
        )

    async def async_authenticate(self, *, force: bool = False) -> tuple[str, str]:
        """Authenticate against OmaWatti and cache the JWT token."""
        async with self._auth_lock:
            if not force and self._token_is_valid():
                assert self._token is not None and self._customer_number is not None
                return self._token, self._customer_number

            url = f"{self._base_url}/{ENDPOINT_AUTHENTICATE}"
            payload = {
                "credentials": {
                    "userID": self._username,
                    "password": self._password,
                    "mobileDevice": False,
                }
            }
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json",
            }
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    async with self._session.post(
                        url, json=payload, headers=headers
                    ) as resp:
                        if resp.status in (401, 403):
                            raise OmaWattiAuthError(
                                f"Invalid OmaWatti credentials (HTTP {resp.status})"
                            )
                        if resp.status >= 400:
                            text = await resp.text()
                            # OmaWatti may return 400/404 for unknown userID
                            if resp.status in (400, 404):
                                raise OmaWattiAuthError(
                                    f"OmaWatti login rejected (HTTP {resp.status})"
                                )
                            raise OmaWattiApiError(
                                f"OmaWatti auth failed (HTTP {resp.status}): {text[:200]}"
                            )
                        body = await resp.json(content_type=None)
            except (OmaWattiAuthError, OmaWattiApiError):
                raise
            except TimeoutError as err:
                raise OmaWattiApiError("Timeout authenticating to OmaWatti") from err
            except Exception as err:
                raise OmaWattiApiError(f"Network error during OmaWatti auth: {err}") from err

            if not isinstance(body, dict) or not body.get("success"):
                raise OmaWattiAuthError("OmaWatti authentication unsuccessful")

            data = body.get("data") or {}
            token = str(data.get("token") or "").strip()
            customer_number = str(data.get("customerNumber") or "").strip()
            if not token or not customer_number:
                raise OmaWattiAuthError("OmaWatti response missing token or customerNumber")

            self._token = token
            self._customer_number = customer_number
            self._token_obtained_at = time.monotonic()
            return token, customer_number

    async def _async_get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        _allow_reauth: bool = True,
    ) -> dict[str, Any]:
        """Perform an authenticated GET request with automatic single 401 retry."""
        token, _ = await self.async_authenticate()
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = {
            "x-frends-bearer-token": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.get(
                    url, params=params, headers=headers
                ) as resp:
                    if resp.status == 401 and _allow_reauth:
                        await self.async_authenticate(force=True)
                        return await self._async_get_json(
                            endpoint, params=params, _allow_reauth=False
                        )
                    if resp.status in (401, 403):
                        raise OmaWattiAuthError(
                            f"Unauthorized calling {endpoint} (HTTP {resp.status})"
                        )
                    if resp.status >= 400:
                        text = await resp.text()
                        raise OmaWattiApiError(
                            f"OmaWatti {endpoint} failed (HTTP {resp.status}): {text[:200]}"
                        )
                    body = await resp.json(content_type=None)
        except (OmaWattiAuthError, OmaWattiApiError):
            raise
        except TimeoutError as err:
            raise OmaWattiApiError(f"Timeout calling OmaWatti {endpoint}") from err
        except Exception as err:
            raise OmaWattiApiError(
                f"Network error calling OmaWatti {endpoint}: {err}"
            ) from err

        if not isinstance(body, dict):
            raise OmaWattiApiError(f"Unexpected non-object JSON from {endpoint}")
        if body.get("success") is False:
            raise OmaWattiApiError(
                f"OmaWatti {endpoint} reported error: {body.get('errors')}"
            )
        return body

    async def async_get_contracts(self) -> list[ContractInfo]:
        """Fetch and normalize all active ELE/HEA contracts."""
        body = await self._async_get_json(ENDPOINT_CONTRACTS)
        raw_contracts = (body.get("data") or {}).get("contracts") or []
        if not isinstance(raw_contracts, list):
            return []
        return parse_active_contracts(raw_contracts)

    async def async_fetch_readings(
        self,
        endpoint: str,
        metering_point: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        """Fetch raw readings for a single date range (inclusive)."""
        _, customer_number = await self.async_authenticate()
        params = {
            "usageplacenumber": metering_point,
            "datefrom": date_from.isoformat(),
            "dateto": date_to.isoformat(),
            "customerNumber": customer_number,
        }
        body = await self._async_get_json(endpoint, params=params)
        readings = (body.get("data") or {}).get("readings") or []
        return readings if isinstance(readings, list) else []

    async def async_fetch_range(
        self,
        endpoint: str,
        metering_point: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        """Fetch raw readings across multi-year ranges split into yearly chunks."""
        all_readings: list[dict[str, Any]] = []
        for chunk_start, chunk_end in iter_year_chunks(date_from, date_to):
            chunk = await self.async_fetch_readings(
                endpoint, metering_point, chunk_start, chunk_end
            )
            all_readings.extend(chunk)
        return all_readings
