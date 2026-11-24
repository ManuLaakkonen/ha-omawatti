"""Manual sync button entity for OmaWatti (Lahti Energia)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OmaWattiCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OmaWatti manual refresh button."""
    coordinator: OmaWattiCoordinator = entry.runtime_data
    async_add_entities([OmaWattiRefreshButton(coordinator, entry)])


class OmaWattiRefreshButton(
    CoordinatorEntity[OmaWattiCoordinator], ButtonEntity
):
    """Button to trigger an immediate OmaWatti data refresh."""

    _attr_has_entity_name = True
    _attr_name = "Hae tiedot nyt"
    _attr_icon = "mdi:cloud-refresh"

    def __init__(
        self,
        coordinator: OmaWattiCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="OmaWatti (Lahti Energia)",
            manufacturer="Lahti Energia",
            model="OmaWatti API",
        )

    async def async_press(self) -> None:
        """Trigger immediate coordinator refresh."""
        await self.coordinator.async_request_refresh()
