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

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Keep external calls from hanging forever.
REQUEST_TIMEOUT_SECONDS = 5.0


class CityNotFoundError(Exception):
    """Raised when the geocoding API has no match for the given city name."""


class WeatherServiceError(Exception):
    """Raised when a call to Open-Meteo fails (network error, bad status, etc.)."""


def geocode_city(city: str) -> dict:
    """Look up a city name and return its location info.

    Returns a dict with at least "latitude", "longitude", and
    optionally "country" (Open-Meteo omits it for some places).
    """
    try:
        response = httpx.get(
            GEOCODING_URL,
            params={"name": city, "count": 1},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        # Covers connection errors, timeouts, and non-2xx status codes.
        raise WeatherServiceError("Failed to reach the geocoding API") from None

    data = response.json()
    results = data.get("results") or []
    if not results:
        raise CityNotFoundError(f"No location found for city '{city}'")

    return results[0]


def get_current_weather(latitude: float, longitude: float) -> dict:
    """Fetch the current weather for a given location."""
    try:
        response = httpx.get(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current_weather": "true",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
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
