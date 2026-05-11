"""Constants for the Open-M2M integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "open_m2m"

# Open-M2M portal API base. All v1 calls are POST with
# ``application/x-www-form-urlencoded`` bodies that must include ``apikey``.
BASE_URL = "https://portal.open-m2m.com/API/v1"

# Wire field name expected by the Open-M2M API. Distinct from HA's
# ``homeassistant.const.CONF_API_KEY`` ("api_key") which is the *storage* key
# inside ``ConfigEntry.data``.
API_FIELD_APIKEY = "apikey"

DEFAULT_TIMEOUT = 10  # seconds
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)

# Endpoint paths (relative to ``BASE_URL``). Names match OpenAPI v1.0.7 (SwaggerHub).
ENDPOINT_TEST_API = "/TestAPI"
ENDPOINT_ACCOUNT_INFO = "/GetAccountInfo"
ENDPOINT_SIMS = "/GetSIMs"
# Subscriptions / bundles (OpenAPI v1.0.7)
ENDPOINT_GET_SUBSCRIPTIONS = "/GetSubscriptions"
ENDPOINT_GET_SUBSCRIPTION_INFO = "/GetSubscriptionInfo"
ENDPOINT_GET_DATABUNDLES = "/GetDatabundles"
ENDPOINT_GET_VOLUME_GROUPS = "/GetVolumeGroups"
ENDPOINT_SUSPEND_SIM = "/SuspendSIM"
ENDPOINT_UNSUSPEND_SIM = "/UnsuspendSIM"
