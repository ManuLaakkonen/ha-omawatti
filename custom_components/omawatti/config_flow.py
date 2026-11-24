"""Config flow for OmaWatti (Lahti Energia) integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ContractInfo, OmaWattiApiError, OmaWattiAuthError, OmaWattiClient
from .const import (
    CONF_CONTRACTS,
    CONF_CUSTOMER_NUMBER,
    CONF_INITIAL_START_DATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class OmaWattiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OmaWatti (Lahti Energia)."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._customer_number: str = ""
        self._contracts: list[ContractInfo] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Authenticate with OmaWatti credentials and discover meters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            session = async_get_clientsession(self.hass)
            client = OmaWattiClient(session, username, password)

            try:
                _, customer_number = await client.async_authenticate()
                contracts = await client.async_get_contracts()
            except OmaWattiAuthError:
                errors["base"] = "invalid_auth"
            except OmaWattiApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during OmaWatti setup")
                errors["base"] = "unknown"
            else:
                if not contracts:
                    errors["base"] = "no_contracts"
                else:
                    await self.async_set_unique_id(f"{DOMAIN}_{customer_number}")
                    self._abort_if_unique_id_configured()
                    self._username = username
                    self._password = password
                    self._customer_number = customer_number
                    self._contracts = contracts
                    return await self.async_step_contracts()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                        autocomplete="username",
                    )
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Select which active meters to track and initial backfill start date."""
        errors: dict[str, str] = {}
        options = [
            selector.SelectOptionDict(
                value=f"{c.usage_place_type}:{c.metering_point}",
                label=c.ui_label,
            )
            for c in self._contracts
        ]
        all_keys = [opt["value"] for opt in options]
        default_start = f"{date.today().year}-01-01"

        if user_input is not None:
            selected_keys: list[str] = list(user_input.get(CONF_CONTRACTS) or [])
            raw_start = str(user_input.get(CONF_INITIAL_START_DATE) or default_start)

            if not selected_keys:
                errors[CONF_CONTRACTS] = "select_at_least_one"
            else:
                try:
                    parsed_start = date.fromisoformat(raw_start)
                    if parsed_start > date.today():
                        errors[CONF_INITIAL_START_DATE] = "future_date"
                except ValueError:
                    errors[CONF_INITIAL_START_DATE] = "invalid_date"

            if not errors:
                by_key = {
                    f"{c.usage_place_type}:{c.metering_point}": c
                    for c in self._contracts
                }
                selected_contracts = [
                    by_key[k].to_config_dict() for k in selected_keys if k in by_key
                ]
                return self.async_create_entry(
                    title="OmaWatti (Lahti Energia)",
                    data={
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_CUSTOMER_NUMBER: self._customer_number,
                        CONF_CONTRACTS: selected_contracts,
                        CONF_INITIAL_START_DATE: raw_start,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_CONTRACTS, default=all_keys): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_INITIAL_START_DATE, default=default_start
                ): selector.DateSelector(),
            }
        )
        return self.async_show_form(
            step_id="contracts", data_schema=schema, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when credentials expire or change."""
        self._username = str(entry_data.get(CONF_USERNAME, ""))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm updated password during re-authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            session = async_get_clientsession(self.hass)
            client = OmaWattiClient(session, username, password)
            try:
                await client.async_authenticate()
            except OmaWattiAuthError:
                errors["base"] = "invalid_auth"
            except OmaWattiApiError:
                errors["base"] = "cannot_connect"
            else:
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                        autocomplete="username",
                    )
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )
