"""Tests for Open-Meteo timeout/retry behavior
(app/services/weather_service.py).

No dedicated test file existed for this module before: every existing
test only ever monkeypatches the top-level get_weather_for_city,
bypassing httpx entirely. These exercise the actual retry/timeout
wiring by monkeypatching weather_service.httpx.get itself.
"""

import httpx
import pytest

from app.services import weather_service


def _response(json_data, status_code=200):
    request = httpx.Request("GET", "https://example.invalid")
    return httpx.Response(status_code, json=json_data, request=request)


def test_geocode_retries_once_on_a_transport_error_then_succeeds(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return _response({"results": [{"name": "London", "latitude": 51.5, "longitude": -0.1, "country": "UK"}]})

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)

    result = weather_service.geocode_city("London")

    assert result["name"] == "London"
    assert len(calls) == 2


def test_geocode_gives_up_after_the_one_bounded_retry(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)

    with pytest.raises(weather_service.WeatherServiceError):
        weather_service.geocode_city("London")

    assert len(calls) == 2  # one initial attempt + exactly one retry


def test_timeout_error_is_also_retried_once(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("boom", request=httpx.Request("GET", url))
        return _response({"results": [{"name": "London", "latitude": 51.5, "longitude": -0.1}]})

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)

    result = weather_service.geocode_city("London")

    assert result["name"] == "London"
    assert len(calls) == 2


def test_http_status_error_is_never_retried(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        return _response({}, status_code=500)

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)

    with pytest.raises(weather_service.WeatherServiceError):
        weather_service.geocode_city("London")

    assert len(calls) == 1  # never retried


def test_forecast_retries_once_on_a_transport_error_then_succeeds(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))
        return _response({"current_weather": {"temperature": 20.0, "windspeed": 5.0, "weathercode": 1}})

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)

    result = weather_service.get_current_weather(51.5, -0.1)

    assert result["temperature"] == 20.0
    assert len(calls) == 2


def test_timeout_value_is_sourced_from_config(monkeypatch):
    captured = {}

    def fake_get(url, params, timeout):
        captured["timeout"] = timeout
        return _response({"results": [{"name": "London", "latitude": 51.5, "longitude": -0.1}]})

    monkeypatch.setattr(weather_service.httpx, "get", fake_get)
    monkeypatch.setattr(weather_service, "OPEN_METEO_TIMEOUT_SECONDS", 2.5)

    weather_service.geocode_city("London")

    assert captured["timeout"] == 2.5
