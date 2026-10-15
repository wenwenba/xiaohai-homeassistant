"""Constants for the Xiaohai Home Assistant integration."""

DOMAIN = "xiaohai"

# Shipped with 小海智家 2.1.8. These identify the app to its own cloud.
# They are not a user password.
CLIENT_ID = "v54yv7ycek7pq74eu3sg"
APP_SECRET = "apca4uafmcadnvsdxjrj4k5v5qecvpve"
# Key recovered from assets/t_s.bmp for this client id.
ASSET_KEY = "rejhmq887sh7j7aep7fjgmqk9x3fs8rc"
# libjnimain builds this when the security flag is off.
SIGN_KEY = f"A_{ASSET_KEY}_{APP_SECRET}"
API_URL = "https://smarthome-api.coli688.com/api.json"
API_VERSION = "1.0"
ET_VERSION = "0.0.1"
USER_AGENT = "TY-UA=APP/Android/2.1.8/SDK/null"

CONF_COUNTRY_CODE = "country_code"
DEFAULT_COUNTRY_CODE = "86"

PLATFORMS = ["climate", "fan", "sensor", "binary_sensor"]
SCAN_INTERVAL_SECONDS = 30
