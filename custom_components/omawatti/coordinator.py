"""DataUpdateCoordinator and recorder external-statistics writer for OmaWatti."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    EnergyConverter,
    TemperatureConverter,
    VolumeConverter,
)

from .api import (
    HourlyPoint,
    MetricSummary,
    OmaWattiApiError,
    OmaWattiAuthError,
    OmaWattiClient,
    aggregate_quarterly_to_hourly,
    build_cumulative_statistics,
    build_mean_statistics,
    compute_delta_t,
    compute_metric_summary,
    parse_hourly_readings,
)
from .const import (
    CONF_CONTRACTS,
    CONF_DISPLAY_NAME,
    CONF_INITIAL_START_DATE,
    CONF_METERING_POINT,
    CONF_SLUG,
    CONF_USAGE_PLACE_TYPE,
    DEFAULT_LOOKBACK_DAYS,
    DOMAIN,
    ENDPOINT_ELECTRICITY,
    ENDPOINT_HEAT_ENERGY,
    ENDPOINT_HEAT_WATERFLOW,
    ENDPOINT_HEAT_WATERIN,
    ENDPOINT_HEAT_WATEROUT,
    UPDATE_INTERVAL,
    USAGE_TYPE_ELECTRICITY,
    USAGE_TYPE_HEATING,
)

_LOGGER = logging.getLogger(__name__)


def statistic_id_for(slug: str, metric: str) -> str:
    """Build deterministic HA external statistic_id (`omawatti:<slug>_<metric>`)."""
    return f"{DOMAIN}:{slug}_{metric}"


def _to_utc_datetime(value: Any) -> datetime | None:
    """Convert recorder start timestamp (epoch float/int or datetime) to UTC."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    return None


class OmaWattiCoordinator(DataUpdateCoordinator[dict[str, dict[str, MetricSummary]]]):
    """Coordinate OmaWatti polling and Home Assistant external statistics imports."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OmaWattiClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.contracts: list[dict[str, str]] = list(entry.data.get(CONF_CONTRACTS, []))
        self._sync_lock = asyncio.Lock()

        raw_start = entry.data.get(CONF_INITIAL_START_DATE)
        try:
            self.initial_start_date = (
                date.fromisoformat(str(raw_start))
                if raw_start
                else date(dt_util.now().year, 1, 1)
            )
        except ValueError:
            self.initial_start_date = date(dt_util.now().year, 1, 1)

    async def _async_update_data(self) -> dict[str, dict[str, MetricSummary]]:
        """Fetch latest readings, update external statistics, and return summaries."""
        async with self._sync_lock:
            return await self._async_sync_contracts(
                contracts=self.contracts,
                override_start_date=None,
            )

    async def async_import_history(
        self,
        start_date: date,
        metering_point: str | None = None,
    ) -> int:
        """Backfill continuous historical statistics from start_date through today.

        Importing continuously through today ensures cumulative `sum` values stay
        strictly monotonic with zero negative spikes at year boundaries.
        Returns the number of contracts imported.
        """
        today = dt_util.now().date()
        if start_date > today:
            raise ValueError(f"start_date {start_date} cannot be in the future")

        target_contracts = [
            c
            for c in self.contracts
            if not metering_point or c[CONF_METERING_POINT] == metering_point
        ]
        if not target_contracts:
            return 0

        async with self._sync_lock:
            updated = await self._async_sync_contracts(
                contracts=target_contracts,
                override_start_date=start_date,
            )

        merged = dict(self.data or {})
        merged.update(updated)
        self.async_set_updated_data(merged)
        return len(target_contracts)

    async def _async_sync_contracts(
        self,
        *,
        contracts: list[dict[str, str]],
        override_start_date: date | None,
    ) -> dict[str, dict[str, MetricSummary]]:
        """Sync one or more contracts from OmaWatti into HA external statistics."""
        local_tz = dt_util.get_default_time_zone()
        today = dt_util.now().date()
        summaries: dict[str, dict[str, MetricSummary]] = {}

        try:
            await self.client.async_authenticate()
        except OmaWattiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OmaWattiApiError as err:
            raise UpdateFailed(str(err)) from err

        for contract in contracts:
            mp = contract[CONF_METERING_POINT]
            usage_type = contract[CONF_USAGE_PLACE_TYPE]
            slug = contract[CONF_SLUG]
            display_name = contract[CONF_DISPLAY_NAME]

            primary_metric = (
                "electricity" if usage_type == USAGE_TYPE_ELECTRICITY else "heat_energy"
            )
            primary_stat_id = statistic_id_for(slug, primary_metric)

            if override_start_date is not None:
                date_from = override_start_date
            else:
                date_from = await self._async_determine_incremental_start(
                    primary_stat_id, today, local_tz
                )

            _LOGGER.debug(
                "Syncing OmaWatti %s (%s) from %s to %s",
                display_name,
                usage_type,
                date_from,
                today,
            )

            contract_key = f"{usage_type}:{mp}"
            try:
                if usage_type == USAGE_TYPE_ELECTRICITY:
                    summaries[contract_key] = await self._async_sync_electricity(
                        mp=mp,
                        slug=slug,
                        display_name=display_name,
                        date_from=date_from,
                        date_to=today,
                        local_tz=local_tz,
                    )
                elif usage_type == USAGE_TYPE_HEATING:
                    summaries[contract_key] = await self._async_sync_heating(
                        mp=mp,
                        slug=slug,
                        display_name=display_name,
                        date_from=date_from,
                        date_to=today,
                        local_tz=local_tz,
                    )
            except OmaWattiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except OmaWattiApiError as err:
                raise UpdateFailed(
                    f"OmaWatti sync failed for {display_name}: {err}"
                ) from err

        return summaries

    async def _async_determine_incremental_start(
        self,
        primary_stat_id: str,
        today: date,
        local_tz: Any,
    ) -> date:
        """Choose fetch start date: full initial backfill or rolling lookback."""
        last_stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            primary_stat_id,
            False,
            {"sum"},
        )
        rows = last_stats.get(primary_stat_id)
        if not rows:
            return self.initial_start_date

        last_start = _to_utc_datetime(rows[0].get("start"))
        if last_start is None:
            return self.initial_start_date

        last_local_date = last_start.astimezone(local_tz).date()
        rolling_start = today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        catchup_start = last_local_date - timedelta(days=2)
        return min(rolling_start, catchup_start)

    async def _async_get_anchor_sum(
        self,
        stat_id: str,
        first_new_hour_utc: datetime,
    ) -> float:
        """Return cumulative sum immediately strictly before `first_new_hour_utc`."""
        recorder = get_instance(self.hass)
        window_start = first_new_hour_utc - timedelta(days=35)
        period_stats = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            first_new_hour_utc,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = period_stats.get(stat_id) or []
        prior_rows: list[tuple[datetime, float]] = []
        for row in rows:
            row_dt = _to_utc_datetime(row.get("start"))
            row_sum = row.get("sum")
            if row_dt is not None and row_dt < first_new_hour_utc and row_sum is not None:
                prior_rows.append((row_dt, float(row_sum)))

        if prior_rows:
            prior_rows.sort(key=lambda item: item[0])
            return prior_rows[-1][1]

        # Fallback for long meter gaps (>35 days) without scanning full DB history.
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            stat_id,
            False,
            {"sum"},
        )
        last_rows = last_stats.get(stat_id) or []
        if last_rows:
            last_dt = _to_utc_datetime(last_rows[0].get("start"))
            last_sum = last_rows[0].get("sum")
            if last_dt is not None and last_dt < first_new_hour_utc and last_sum is not None:
                return float(last_sum)

        return 0.0

    async def _async_push_cumulative(
        self,
        *,
        stat_id: str,
        name: str,
        unit: str,
        unit_class: str,
        points: list[HourlyPoint],
    ) -> None:
        """Write cumulative hourly external statistics cleanly anchored to DB history."""
        if not points:
            return
        anchor_sum = await self._async_get_anchor_sum(stat_id, points[0].start)
        stat_rows = [
            StatisticData(**row)
            for row in build_cumulative_statistics(points, initial_sum=anchor_sum)
        ]
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        async_add_external_statistics(self.hass, metadata, stat_rows)

    def _push_mean(
        self,
        *,
        stat_id: str,
        name: str,
        unit: str,
        unit_class: str,
        points: list[HourlyPoint],
    ) -> None:
        """Write hourly mean external statistics (e.g. supply/return temperatures)."""
        if not points:
            return
        stat_rows = [StatisticData(**row) for row in build_mean_statistics(points)]
        metadata = StatisticMetaData(
            has_sum=False,
            mean_type=StatisticMeanType.ARITHMETIC,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        async_add_external_statistics(self.hass, metadata, stat_rows)

    async def _async_sync_electricity(
        self,
        *,
        mp: str,
        slug: str,
        display_name: str,
        date_from: date,
        date_to: date,
        local_tz: Any,
    ) -> dict[str, MetricSummary]:
        """Fetch 15-min electricity readings, roll up to UTC hours, write LTS."""
        raw = await self.client.async_fetch_range(
            ENDPOINT_ELECTRICITY, mp, date_from, date_to
        )
        hourly = aggregate_quarterly_to_hourly(raw)
        await self._async_push_cumulative(
            stat_id=statistic_id_for(slug, "electricity"),
            name=f"OmaWatti {display_name} Sähkö",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class=EnergyConverter.UNIT_CLASS,
            points=hourly,
        )
        return {"electricity": compute_metric_summary(hourly, local_tz=local_tz)}

    async def _async_sync_heating(
        self,
        *,
        mp: str,
        slug: str,
        display_name: str,
        date_from: date,
        date_to: date,
        local_tz: Any,
    ) -> dict[str, MetricSummary]:
        """Fetch district heating energy, flow, supply/return temps, and delta T."""
        raw_energy, raw_flow, raw_in, raw_out = await asyncio.gather(
            self.client.async_fetch_range(ENDPOINT_HEAT_ENERGY, mp, date_from, date_to),
            self.client.async_fetch_range(ENDPOINT_HEAT_WATERFLOW, mp, date_from, date_to),
            self.client.async_fetch_range(ENDPOINT_HEAT_WATERIN, mp, date_from, date_to),
            self.client.async_fetch_range(ENDPOINT_HEAT_WATEROUT, mp, date_from, date_to),
        )

        # OmaWatti heat-energy is in MWh -> convert to kWh for Home Assistant
        energy_kwh = parse_hourly_readings(raw_energy, multiplier=1000.0)
        waterflow_m3 = parse_hourly_readings(raw_flow, multiplier=1.0)
        water_in_c = parse_hourly_readings(raw_in, multiplier=1.0)
        water_out_c = parse_hourly_readings(raw_out, multiplier=1.0)
        delta_t_c = compute_delta_t(water_in_c, water_out_c)

        await self._async_push_cumulative(
            stat_id=statistic_id_for(slug, "heat_energy"),
            name=f"OmaWatti {display_name} Kaukolämpö",
            unit=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class=EnergyConverter.UNIT_CLASS,
            points=energy_kwh,
        )
        await self._async_push_cumulative(
            stat_id=statistic_id_for(slug, "heat_waterflow"),
            name=f"OmaWatti {display_name} Vesivirtaus",
            unit=UnitOfVolume.CUBIC_METERS,
            unit_class=VolumeConverter.UNIT_CLASS,
            points=waterflow_m3,
        )
        self._push_mean(
            stat_id=statistic_id_for(slug, "heat_water_in"),
            name=f"OmaWatti {display_name} Menovesi",
            unit=UnitOfTemperature.CELSIUS,
            unit_class=TemperatureConverter.UNIT_CLASS,
            points=water_in_c,
        )
        self._push_mean(
            stat_id=statistic_id_for(slug, "heat_water_out"),
            name=f"OmaWatti {display_name} Paluuvesi",
            unit=UnitOfTemperature.CELSIUS,
            unit_class=TemperatureConverter.UNIT_CLASS,
            points=water_out_c,
        )
        self._push_mean(
            stat_id=statistic_id_for(slug, "heat_delta_t"),
            name=f"OmaWatti {display_name} Jäähtymä ΔT",
            unit=UnitOfTemperature.CELSIUS,
            unit_class=TemperatureConverter.UNIT_CLASS,
            points=delta_t_c,
        )

        return {
            "heat_energy": compute_metric_summary(energy_kwh, local_tz=local_tz),
            "heat_waterflow": compute_metric_summary(waterflow_m3, local_tz=local_tz),
            "heat_water_in": compute_metric_summary(water_in_c, local_tz=local_tz),
            "heat_water_out": compute_metric_summary(water_out_c, local_tz=local_tz),
            "heat_delta_t": compute_metric_summary(delta_t_c, local_tz=local_tz),
        }
