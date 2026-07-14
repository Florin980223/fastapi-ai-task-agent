"""HTTP endpoints for third-party integrations.

Like routes/tasks.py, this file only handles request/response wiring
(query params, HTTP status codes/errors) and delegates the real work
to app.services.weather_service.
"""

from fastapi import APIRouter, HTTPException

from app.schemas import WeatherResponse
from app.services import weather_service
from app.services.weather_service import CityNotFoundError, WeatherServiceError

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/weather", response_model=WeatherResponse)
def get_weather(city: str):
    try:
        return weather_service.get_weather_for_city(city)
    except CityNotFoundError:
        raise HTTPException(status_code=404, detail="City not found")
    except WeatherServiceError:
        raise HTTPException(status_code=502, detail="Weather service unavailable")
