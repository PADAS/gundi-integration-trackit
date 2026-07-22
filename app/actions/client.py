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
    # Any other status (400/422/429/...) still means TrackIt was reached and
    # answered — surface it as an API error, not a transport failure.
    raise TrackitBaseException(f"TrackIt returned HTTP {status}", e, status_code=status) from e


async def _post(session, base_url, endpoint, *, params=None, headers=None, json=None) -> dict:
    """POST to the TrackIt webservice and return the parsed JSON body.

    Centralizes error-status mapping and guards against non-JSON responses
    (the query-string-routed endpoint returns HTML/plain text on some errors),
    which would otherwise raise an unhandled JSONDecodeError out of the caller.
    """
    request_params = {"token": endpoint}
    if params:
        request_params.update(params)
    try:
        response = await session.post(base_url, params=request_params, headers=headers, json=json)
        if response.is_error:
            logger.error(f"Error in '{endpoint}' endpoint. Response body: {response.text}")
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        handle_httpx_error(e)

    try:
        return response.json()
    except ValueError as e:
        raise TrackitBaseException(
            f"TrackIt '{endpoint}' endpoint returned a non-JSON response: {response.text[:500]}", e
        ) from e


class TrackitVehicle(pydantic.BaseModel):
    imei: str = pydantic.Field(alias="Imeino")
    latitude: Optional[float] = pydantic.Field(default=None, alias="Latitude")
    longitude: Optional[float] = pydantic.Field(default=None, alias="Longitude")
    gps_actual_time: Optional[datetime] = pydantic.Field(default=None, alias="GPSActualTime")
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

    @pydantic.validator("gps_actual_time", pre=True)
    def _parse_trackit_datetime(cls, v):
        if v is None or isinstance(v, datetime):
            return v
        try:
            return datetime.strptime(v, GPS_TIME_FORMAT)
        except (ValueError, TypeError):
            # A device that never got a fix can report a malformed time; treat
            # it as missing so the handler skips it rather than failing the row.
            logger.warning(f"Unparseable GPSActualTime '{v}'; treating as missing")
            return None


async def get_token(base_url: str, username: str, password: pydantic.SecretStr) -> str:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        parsed_response = await _post(
            session,
            base_url,
            "generateAccessToken",
            json={"username": username, "password": password.get_secret_value()},
        )

    if not isinstance(parsed_response, dict):
        raise TrackitBaseException(f"Unexpected login response from TrackIt: {parsed_response}")

    token = (parsed_response.get("data") or {}).get("token")
    # The API stringifies numerics elsewhere in its payloads, so accept "1" as
    # well as 1 for the result flag. Both a token and a success flag are
    # required — a token alongside result != 1 is treated as a failed login.
    result_ok = str(parsed_response.get("result")).strip() == "1"
    if not token or not result_ok:
        raise TrackitUnauthorizedException(
            f"Login failed for username {username}. Response: {parsed_response.get('message') or parsed_response}"
        )
    return token


async def get_live_data(
        base_url: str,
        token: str,
        project_id: int,
        company_names: str,
        imei_nos: Optional[str] = None,
) -> List[TrackitVehicle]:
    body = {"company_names": company_names, "format": "json"}
    if imei_nos:
        body["imei_nos"] = imei_nos

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        parsed_response = await _post(
            session,
            base_url,
            "getTokenBaseLiveData",
            params={"ProjectId": project_id},
            headers={"auth-code": token},
            json=body,
        )

    if not isinstance(parsed_response, dict) or "root" not in parsed_response:
        message = parsed_response.get("message") if isinstance(parsed_response, dict) else str(parsed_response)
        if message and "no company found" in str(message).lower():
            raise TrackitNotFoundException(f"No Company Found for company_names '{company_names}'")
        raise TrackitBaseException(f"Unexpected response from TrackIt live data endpoint: {parsed_response}")

    raw_vehicles = (parsed_response.get("root") or {}).get("VehicleData") or []

    # Parse each vehicle independently so one malformed row (e.g. a null IMEI or
    # an unparseable field) is skipped rather than aborting the whole pull.
    vehicles = []
    for raw in raw_vehicles:
        try:
            vehicles.append(TrackitVehicle.parse_obj(raw))
        except pydantic.ValidationError as e:
            logger.warning(f"Skipping malformed vehicle row (imei={raw.get('Imeino')!r}): {e}")
    return vehicles
