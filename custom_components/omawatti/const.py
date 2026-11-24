"""Constants for the OmaWatti (Lahti Energia) integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "omawatti"

API_BASE_URL = "https://omawatti.lahtienergia.fi/backend/api/v1/enduser"

ENDPOINT_AUTHENTICATE = "authenticate"
ENDPOINT_CONTRACTS = "contracts"
ENDPOINT_ELECTRICITY = "electricity-consumption"
ENDPOINT_HEAT_ENERGY = "heat-energy"
ENDPOINT_HEAT_WATERFLOW = "heat-waterflow"
ENDPOINT_HEAT_WATERIN = "heat-waterin"
ENDPOINT_HEAT_WATEROUT = "heat-waterout"

USAGE_TYPE_ELECTRICITY = "ELE"
USAGE_TYPE_HEATING = "HEA"

CONF_CONTRACTS = "contracts"
CONF_INITIAL_START_DATE = "initial_start_date"
CONF_CUSTOMER_NUMBER = "customer_number"
CONF_METERING_POINT = "metering_point"
CONF_USAGE_PLACE_TYPE = "usage_place_type"
CONF_SLUG = "slug"
CONF_DISPLAY_NAME = "display_name"
CONF_STREET_NAME = "street_name"
CONF_CITY = "city"
CONF_PRODUCT = "product"

DEFAULT_LOOKBACK_DAYS = 10
TOKEN_TTL_SECONDS = 3000  # Refresh proactively before 3600 s JWT expiry
REQUEST_TIMEOUT_SECONDS = 60
UPDATE_INTERVAL = timedelta(hours=6)

SERVICE_IMPORT_HISTORY = "import_history"
ATTR_START_DATE = "start_date"
ATTR_METERING_POINT = "metering_point"
