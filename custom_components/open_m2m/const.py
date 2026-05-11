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

# Cap normalized databundle rows per coordinator refresh to limit sensor fan-out.
MAX_DATABUNDLES = 20

# Endpoint paths (relative to ``BASE_URL``). Names match OpenAPI v1.0.7 (SwaggerHub).
ENDPOINT_TEST_API = "/TestAPI"
ENDPOINT_ACCOUNT_INFO = "/GetAccountInfo"
ENDPOINT_SIMS = "/GetSIMs"
# Single-SIM detail (OpenAPI v1.0.7); response wraps the row in ``SIM``.
ENDPOINT_GET_SIM = "/GetSIM"
ENDPOINT_SIM_DETAIL = ENDPOINT_GET_SIM  # OpenAPI name ``GetSIM``
# Cap ``GetSIM`` calls per coordinator refresh (enrichment when list lacks description).
MAX_SIM_DETAIL_ENRICHMENT_PER_REFRESH = 5
# Cap ``GetProductInfo`` calls per coordinator refresh (unique ``product_id``).
MAX_PRODUCT_INFO_FETCHES = 5
# Cap ``GetUsageTotals`` subscription-scoped retries when account-level call yields no CDR.
MAX_USAGE_TOTALS_SUBSCRIPTION_TRIES = 3
# Subscriptions / bundles (OpenAPI v1.0.7)
ENDPOINT_GET_SUBSCRIPTIONS = "/GetSubscriptions"
ENDPOINT_GET_SUBSCRIPTION_INFO = "/GetSubscriptionInfo"
# Product catalog row (OpenAPI v1.0.7); POST body uses ``product_id`` (snake_case),
# same style as ``subscription_id`` on other endpoints (see README / SwaggerHub).
ENDPOINT_PRODUCT_INFO = "/GetProductInfo"
ENDPOINT_GET_DATABUNDLES = "/GetDatabundles"
ENDPOINT_GET_VOLUME_GROUPS = "/GetVolumeGroups"
ENDPOINT_USAGE_TOTALS = "/GetUsageTotals"
ENDPOINT_SUSPEND_SIM = "/SuspendSIM"
ENDPOINT_UNSUSPEND_SIM = "/UnsuspendSIM"
