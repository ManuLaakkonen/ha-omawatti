"""Summary sensor entities for OmaWatti (Lahti Energia) dashboard tiles.

Note: True historical hourly consumption and temperatures are stored directly in
Home Assistant's recorder as External Statistics (`omawatti:<slug>_<metric>`).
These sensors intentionally omit `state_class` so HA does not create duplicate
wall-clock-stamped statistics alongside the accurate historical external stats.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MetricSummary
from .const import (
    CONF_DISPLAY_NAME,
    CONF_METERING_POINT,
    CONF_PRODUCT,
    CONF_SLUG,
    CONF_USAGE_PLACE_TYPE,
    DOMAIN,
    USAGE_TYPE_ELECTRICITY,
)
from .coordinator import OmaWattiCoordinator, statistic_id_for


@dataclass(frozen=True, kw_only=True)
class OmaWattiSensorDescription(SensorEntityDescription):
    """Describe an OmaWatti summary sensor."""

    metric_key: str
    value_fn: Callable[[MetricSummary], float | None]
    attrs_fn: Callable[[MetricSummary, str], dict[str, Any]]


def _daily_energy_attrs(summary: MetricSummary, stat_id: str) -> dict[str, Any]:
    return {
        "statistic_id": stat_id,
        "date": summary.latest_day_date,
        "hours_count": summary.latest_day_hours,
        "last_7_days": summary.last_7d_total,
        "latest_reading_time": summary.latest_timestamp,
    }


def _rolling_7d_attrs(summary: MetricSummary, stat_id: str) -> dict[str, Any]:
    return {
        "statistic_id": stat_id,
        "end_date": summary.latest_day_date,
        "latest_reading_time": summary.latest_timestamp,
    }


def _temperature_attrs(summary: MetricSummary, stat_id: str) -> dict[str, Any]:
    return {
        "statistic_id": stat_id,
        "daily_avg_c": summary.latest_day_avg,
        "date": summary.latest_day_date,
        "latest_reading_time": summary.latest_timestamp,
    }


ELE_SENSORS: tuple[OmaWattiSensorDescription, ...] = (
    OmaWattiSensorDescription(
        key="electricity_latest_day",
        name="Sähkö (viimeisin päivä)",
        metric_key="electricity",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:flash",
        value_fn=lambda s: s.latest_day_total,
        attrs_fn=_daily_energy_attrs,
    ),
    OmaWattiSensorDescription(
        key="electricity_last_7d",
        name="Sähkö (7 päivää)",
        metric_key="electricity",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:calendar-week",
        value_fn=lambda s: s.last_7d_total,
        attrs_fn=_rolling_7d_attrs,
    ),
)

HEA_SENSORS: tuple[OmaWattiSensorDescription, ...] = (
    OmaWattiSensorDescription(
        key="heat_energy_latest_day",
        name="Kaukolämpö (viimeisin päivä)",
        metric_key="heat_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:fire",
        value_fn=lambda s: s.latest_day_total,
        attrs_fn=_daily_energy_attrs,
    ),
    OmaWattiSensorDescription(
        key="heat_energy_last_7d",
        name="Kaukolämpö (7 päivää)",
        metric_key="heat_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:calendar-week",
        value_fn=lambda s: s.last_7d_total,
        attrs_fn=_rolling_7d_attrs,
    ),
    OmaWattiSensorDescription(
        key="heat_water_in",
        name="Menovesi",
        metric_key="heat_water_in",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        icon="mdi:thermometer-high",
        value_fn=lambda s: s.latest_value,
        attrs_fn=_temperature_attrs,
    ),
    OmaWattiSensorDescription(
        key="heat_water_out",
        name="Paluuvesi",
        metric_key="heat_water_out",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        icon="mdi:thermometer-low",
        value_fn=lambda s: s.latest_value,
        attrs_fn=_temperature_attrs,
    ),
    OmaWattiSensorDescription(
        key="heat_delta_t",
        name="Jäähtymä (ΔT)",
        metric_key="heat_delta_t",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        icon="mdi:thermometer-lines",
        value_fn=lambda s: s.latest_value,
        attrs_fn=_temperature_attrs,
    ),
    OmaWattiSensorDescription(
        key="heat_waterflow_latest_day",
        name="Vesivirtaus (viimeisin päivä)",
        metric_key="heat_waterflow",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        icon="mdi:water",
        value_fn=lambda s: s.latest_day_total,
        attrs_fn=_daily_energy_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OmaWatti summary sensors for all configured meters."""
    coordinator: OmaWattiCoordinator = entry.runtime_data
    entities: list[OmaWattiSummarySensor] = []

    for contract in coordinator.contracts:
        usage_type = contract[CONF_USAGE_PLACE_TYPE]
        descriptions = (
            ELE_SENSORS if usage_type == USAGE_TYPE_ELECTRICITY else HEA_SENSORS
        )
        for description in descriptions:
            entities.append(OmaWattiSummarySensor(coordinator, contract, description))

    async_add_entities(entities)


class OmaWattiSummarySensor(
    CoordinatorEntity[OmaWattiCoordinator], SensorEntity
):
    """Summary sensor backed by OmaWattiCoordinator."""

    entity_description: OmaWattiSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OmaWattiCoordinator,
        contract: dict[str, str],
        description: OmaWattiSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._metering_point = contract[CONF_METERING_POINT]
        self._slug = contract[CONF_SLUG]
        self._usage_type = contract[CONF_USAGE_PLACE_TYPE]
        self._display_name = contract[CONF_DISPLAY_NAME]
        self._product = contract.get(CONF_PRODUCT, "")
        self._stat_id = statistic_id_for(self._slug, description.metric_key)

        type_label = (
            "Sähkö" if self._usage_type == USAGE_TYPE_ELECTRICITY else "Kaukolämpö"
        )
        self._attr_unique_id = f"{self._slug}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._usage_type}_{self._metering_point}")},
            name=f"OmaWatti {self._display_name} ({type_label})",
            manufacturer="Lahti Energia",
            model=self._product or type_label,
        )

    def _get_summary(self) -> MetricSummary | None:
        contract_key = f"{self._usage_type}:{self._metering_point}"
        meter_data = (self.coordinator.data or {}).get(contract_key)
        if not meter_data:
            return None
        return meter_data.get(self.entity_description.metric_key)

    @property
    def native_value(self) -> float | None:
        summary = self._get_summary()
        if summary is None:
            return None
        return self.entity_description.value_fn(summary)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self._get_summary()
        if summary is None:
            return {"statistic_id": self._stat_id}
        attrs = self.entity_description.attrs_fn(summary, self._stat_id)
        if self._product:
            attrs["product"] = self._product
        return attrs
