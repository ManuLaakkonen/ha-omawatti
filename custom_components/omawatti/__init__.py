"""The OmaWatti (Lahti Energia) Home Assistant integration."""

from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OmaWattiClient
from .const import (
    ATTR_METERING_POINT,
    ATTR_START_DATE,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
)
from .coordinator import OmaWattiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_METERING_POINT): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OmaWatti from a config entry."""
    session = async_get_clientsession(hass)
    client = OmaWattiClient(
        session=session,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    coordinator = OmaWattiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):

        async def _handle_import_history(call: ServiceCall) -> None:
            start_date: date = call.data[ATTR_START_DATE]
            metering_point: str | None = call.data.get(ATTR_METERING_POINT)
            total_imported = 0

            for cfg_entry in hass.config_entries.async_loaded_entries(DOMAIN):
                coord: OmaWattiCoordinator = cfg_entry.runtime_data
                try:
                    total_imported += await coord.async_import_history(
                        start_date=start_date,
                        metering_point=metering_point,
                    )
                except ValueError as err:
                    raise HomeAssistantError(str(err)) from err

            if total_imported == 0:
                raise HomeAssistantError(
                    "No matching OmaWatti meters found for historical import."
                )
            _LOGGER.info(
                "OmaWatti historical import complete from %s (%d meter(s))",
                start_date.isoformat(),
                total_imported,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            _handle_import_history,
            schema=IMPORT_HISTORY_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an OmaWatti config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        remaining = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not remaining and hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
    return unload_ok
