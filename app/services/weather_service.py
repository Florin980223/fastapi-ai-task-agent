"""Logic for talking to the free Open-Meteo weather APIs.

Two separate Open-Meteo endpoints are involved:
1. The geocoding API turns a city name into latitude/longitude.
2. The forecast API turns latitude/longitude into current weather.

We use plain exceptions (instead of returning None, like task_service
does) because there are two distinct failure modes here that need to
map to two different HTTP status codes: "city not found" (404) and
"the weather service is unreachable/broken" (502).
"""

import httpx

from app.config import OPEN_METEO_TIMEOUT_SECONDS

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Exactly one bounded retry, and only for a transport-level failure
# (connection refused, DNS failure, a timeout - httpx.TransportError,
# which covers httpx.TimeoutException too) - never for an HTTP status
# error (httpx.HTTPStatusError, raised by raise_for_status() below):
# Open-Meteo answered with a 4xx/5xx, and won't answer differently on
# retry. Both calls below are idempotent GETs with no side effects, so
# a single retry here is safe; unbounded/exponential-backoff retries
# aren't - one immediate retry, no backoff, is enough to ride out a
# single transient blip without meaningfully slowing down a real
# failure.
_MAX_RETRIES = 1


class CityNotFoundError(Exception):
    """Raised when the geocoding API has no match for the given city name."""


class WeatherServiceError(Exception):
    """Raised when a call to Open-Meteo fails (network error, bad status, etc.)."""


def _get_with_retry(url: str, params: dict) -> httpx.Response:
    attempt = 0
    while True:
        try:
            response = httpx.get(url, params=params, timeout=OPEN_METEO_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response
        except httpx.TransportError:
            if attempt >= _MAX_RETRIES:
                raise
            attempt += 1


def geocode_city(city: str) -> dict:
    """Look up a city name and return its location info.

    Returns a dict with at least "latitude", "longitude", and
    optionally "country" (Open-Meteo omits it for some places).
    """
    try:
        response = _get_with_retry(GEOCODING_URL, {"name": city, "count": 1})
    except httpx.HTTPError:
        # Covers connection errors/timeouts (after the bounded retry
        # above) and non-2xx status codes (never retried).
        raise WeatherServiceError("Failed to reach the geocoding API") from None

    data = response.json()
    results = data.get("results") or []
    if not results:
        raise CityNotFoundError(f"No location found for city '{city}'")

    return results[0]


def get_current_weather(latitude: float, longitude: float) -> dict:
    """Fetch the current weather for a given location."""
    try:
        response = _get_with_retry(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current_weather": "true",
            },
        )
    except httpx.HTTPError:
        raise WeatherServiceError("Failed to reach the forecast API") from None

    data = response.json()
    current_weather = data.get("current_weather")
    if current_weather is None:
        raise WeatherServiceError("Forecast API response is missing current_weather")

    return current_weather


def get_weather_for_city(city: str) -> dict:
    """Look up a city and return its current weather in a simple flat dict.

    Raises CityNotFoundError if the city doesn't exist, or
    WeatherServiceError if either Open-Meteo call fails.
    """
    location = geocode_city(city)
    current_weather = get_current_weather(location["latitude"], location["longitude"])

    return {
        "city": location.get("name", city),
        "country": location.get("country"),
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current_temperature": current_weather["temperature"],
        "wind_speed": current_weather["windspeed"],
        "weather_code": current_weather["weathercode"],
    }
