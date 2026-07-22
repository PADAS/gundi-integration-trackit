import logging

from datetime import datetime
from typing import List, Optional

import httpx
import pydantic


logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=15.0, pool=5.0)
GPS_TIME_FORMAT = "%d-%m-%Y %H:%M:%S"

# Placeholder values the TrackIt API uses for missing data
EMPTY_VALUES = {"", "--", "NA"}


class TrackitBaseException(Exception):
    default_status_code: int = None

    def __init__(self, message: str, error: Exception = None, status_code: int = None):
        self.status_code = status_code if status_code is not None else self.default_status_code
        self.message = message
        self.error = error
        super().__init__(message)

    def __str__(self):
        return f"{self.status_code}: {self.message}, Error: {self.error}"


class TrackitUnauthorizedException(TrackitBaseException):
    default_status_code = 401


class TrackitNotFoundException(TrackitBaseException):
    default_status_code = 404


class TrackitInternalServerException(TrackitBaseException):
    default_status_code = 500


def handle_httpx_error(e: httpx.HTTPStatusError):
    status = e.response.status_code
    if status in (401, 403):
        raise TrackitUnauthorizedException("Unauthorized access", e, status_code=status) from e
    if status == 404:
        raise TrackitNotFoundException("Not found", e) from e
    if status >= 500:
        raise TrackitInternalServerException("Internal server error at TrackIt", e, status_code=status) from e
    raise e


class TrackitVehicle(pydantic.BaseModel):
    imei: str = pydantic.Field(alias="Imeino")
    latitude: Optional[float] = pydantic.Field(default=None, alias="Latitude")
    longitude: Optional[float] = pydantic.Field(default=None, alias="Longitude")
    gps_actual_time: Optional[datetime] = pydantic.Field(default=None, alias="GPSActualTime")
    device_datetime: Optional[datetime] = pydantic.Field(default=None, alias="Datetime")
    vehicle_name: Optional[str] = pydantic.Field(default=None, alias="Vehicle_Name")
    vehicle_no: Optional[str] = pydantic.Field(default=None, alias="Vehicle_No")
    vehicle_type: Optional[str] = pydantic.Field(default=None, alias="Vehicletype")
    device_model: Optional[str] = pydantic.Field(default=None, alias="DeviceModel")
    company: Optional[str] = pydantic.Field(default=None, alias="Company")
    branch: Optional[str] = pydantic.Field(default=None, alias="Branch")
    status: Optional[str] = pydantic.Field(default=None, alias="Status")
    gps: Optional[str] = pydantic.Field(default=None, alias="GPS")
    ignition: Optional[str] = pydantic.Field(default=None, alias="IGN")
    speed: Optional[float] = pydantic.Field(default=None, alias="Speed")
    angle: Optional[float] = pydantic.Field(default=None, alias="Angle")
    altitude: Optional[float] = pydantic.Field(default=None, alias="Altitude")
    odometer: Optional[str] = pydantic.Field(default=None, alias="Odometer")
    battery_percentage: Optional[str] = None
    satellite_count: Optional[int] = None
    location: Optional[str] = pydantic.Field(default=None, alias="Location")
    poi: Optional[str] = pydantic.Field(default=None, alias="POI")
    heartbeat: Optional[str] = None
    power: Optional[str] = pydantic.Field(default=None, alias="Power")
    external_volt: Optional[str] = pydantic.Field(default=None, alias="ExternalVolt")
    temperature: Optional[str] = pydantic.Field(default=None, alias="Temperature")
    sos: Optional[str] = pydantic.Field(default=None, alias="SOS")
    immobilize_state: Optional[str] = pydantic.Field(default=None, alias="Immobilize_State")

    class Config:
        allow_population_by_field_name = True

    @pydantic.root_validator(pre=True)
    def _normalize_empty_values(cls, values):
        return {
            k: (None if isinstance(v, str) and v.strip() in EMPTY_VALUES else v)
            for k, v in values.items()
        }

    @pydantic.validator("gps_actual_time", "device_datetime", pre=True)
    def _parse_trackit_datetime(cls, v):
        if v is None or isinstance(v, datetime):
            return v
        return datetime.strptime(v, GPS_TIME_FORMAT)


class TrackitLiveDataResponse(pydantic.BaseModel):
    vehicle_data: List[TrackitVehicle] = pydantic.Field(default_factory=list, alias="VehicleData")

    class Config:
        allow_population_by_field_name = True


async def get_token(base_url: str, username: str, password: pydantic.SecretStr) -> str:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        try:
            response = await session.post(
                base_url,
                params={"token": "generateAccessToken"},
                json={"username": username, "password": password.get_secret_value()},
            )
            if response.is_error:
                logger.error(f"Error in 'get_token' endpoint. Response body: {response.text}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            handle_httpx_error(e)

        parsed_response = response.json()
        token = (parsed_response.get("data") or {}).get("token")
        if parsed_response.get("result") != 1 or not token:
            raise TrackitUnauthorizedException(
                f"Login failed for username {username}. Response: {parsed_response.get('message') or response.text}"
            )
        return token


async def get_live_data(
        base_url: str,
        token: str,
        project_id: int,
        company_names: str,
        imei_nos: Optional[str] = None,
) -> List[TrackitVehicle]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        body = {"company_names": company_names, "format": "json"}
        if imei_nos:
            body["imei_nos"] = imei_nos

        try:
            response = await session.post(
                base_url,
                params={"token": "getTokenBaseLiveData", "ProjectId": project_id},
                headers={"auth-code": token},
                json=body,
            )
            if response.is_error:
                logger.error(f"Error in 'get_live_data' endpoint. Response body: {response.text}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            handle_httpx_error(e)

        parsed_response = response.json()
        if not isinstance(parsed_response, dict) or "root" not in parsed_response:
            message = parsed_response.get("message") if isinstance(parsed_response, dict) else str(parsed_response)
            if message and "no company found" in str(message).lower():
                raise TrackitNotFoundException(f"No Company Found for company_names '{company_names}'")
            raise TrackitBaseException(f"Unexpected response from TrackIt live data endpoint: {response.text}")

        return TrackitLiveDataResponse.parse_obj(parsed_response.get("root") or {}).vehicle_data
