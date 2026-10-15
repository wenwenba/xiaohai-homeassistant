"""Constants for the Xiaohai Home Assistant integration."""

DOMAIN = "xiaohai"

# Shipped with 小海智家 2.1.8. These identify the app to its own cloud.
# They are not a user password.
CLIENT_ID = "v54yv7ycek7pq74eu3sg"
APP_SECRET = "apca4uafmcadnvsdxjrj4k5v5qecvpve"
API_URL = "https://smarthome-api.coli688.com/api.json"
API_VERSION = "1.0"
USER_AGENT = "TY-UA=APP/Android/2.1.8/SDK/null"

CONF_COUNTRY_CODE = "country_code"
DEFAULT_COUNTRY_CODE = "86"

PLATFORMS = ["switch", "binary_sensor"]
SCAN_INTERVAL_SECONDS = 30
