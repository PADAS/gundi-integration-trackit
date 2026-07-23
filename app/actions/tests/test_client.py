import pytest
import pydantic
import respx

from datetime import datetime

from app.actions.client import (
    get_token,
    get_live_data,
    TrackitBaseException,
    TrackitNotFoundException,
    TrackitUnauthorizedException,
    TrackitInternalServerException,
    TrackitVehicle,
)


BASE_URL = "https://genx.trackit.co.zw/webservice"

VEHICLE_DATA = {
    "Company": "Chewore",
    "heartbeat": "no",
    "Latitude": "-17.8103899",
    "GPS": "ON",
    "Status": "STOP",
    "DeviceModel": "FMB920",
    "AC": "--",
    "gps_hdop": "NA",
    "Odometer": "59730724",
    "POI": "At 24 Princess Drive",
    "Longitude": "31.08255",
    "satellite_count": 18,
    "ExternalVolt": "--",
    "Vehicle_Name": "AFO 1285",
    "Vehicle_No": "AFO 1285",
    "Branch": "Chewore",
    "Vehicletype": "Car",
    "course": "",
    "GPSActualTime": "21-07-2026 09:31:31",
    "Datetime": "21-07-2026 09:31:38",
    "Speed": "0",
    "Imeino": "353742376164273",
    "IGN": "OFF",
    "Angle": "76",
    "Fuel": [],
    "Vin": "--",
    "battery_percentage": "0",
    "Power": "--",
    "username": "user@test.org",
    "Location": "At 24 Princess Drive",
    "Altitude": "1492",
}


@pytest.mark.asyncio
@respx.mock
async def test_get_token_success():
    route = respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(
        json={"result": 1, "data": {"token": "token123"}, "message": ""}
    )

    token = await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("secret"))

    assert route.called
    assert token == "token123"


@pytest.mark.asyncio
@respx.mock
async def test_get_token_bad_credentials():
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(
        json={"result": 0, "data": {}, "message": "Invalid username or password"}
    )

    with pytest.raises(TrackitUnauthorizedException):
        await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("bad"))


@pytest.mark.asyncio
@respx.mock
async def test_get_token_failure_message_does_not_leak_credentials():
    # A failed login must never surface the username or any token from the
    # raw response in the exception message (it reaches portal/activity logs).
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(
        json={"result": 0, "data": {"token": "leaked-token-abc"}, "message": ""}
    )

    with pytest.raises(TrackitUnauthorizedException) as exc_info:
        await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("bad"))

    assert "leaked-token-abc" not in str(exc_info.value)
    assert "user@test.org" not in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_get_token_server_error():
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(status_code=500)

    with pytest.raises(TrackitInternalServerException):
        await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("secret"))


@pytest.mark.asyncio
@respx.mock
async def test_get_token_unexpected_4xx_maps_to_trackit_error():
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(status_code=429)

    with pytest.raises(TrackitBaseException) as exc_info:
        await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("secret"))

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
@respx.mock
async def test_get_token_accepts_stringified_result():
    # The API stringifies numerics elsewhere; "1" must still count as success.
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(
        json={"result": "1", "data": {"token": "token123"}, "message": ""}
    )

    token = await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("secret"))

    assert token == "token123"


@pytest.mark.asyncio
@respx.mock
async def test_get_token_non_json_body_raises_trackit_error():
    respx.post(BASE_URL, params={"token": "generateAccessToken"}).respond(
        status_code=200, text="<html>Invalid Request</html>"
    )

    with pytest.raises(TrackitBaseException):
        await get_token(BASE_URL, "user@test.org", pydantic.SecretStr("secret"))


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_success():
    route = respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(
        json={"root": {"VehicleData": [VEHICLE_DATA]}}
    )

    vehicles = await get_live_data(BASE_URL, "token123", 37, "Chewore", "353742376164273")

    assert route.called
    request = route.calls.last.request
    assert request.headers["auth-code"] == "token123"

    assert len(vehicles) == 1
    vehicle = vehicles[0]
    assert vehicle.imei == "353742376164273"
    assert vehicle.latitude == -17.8103899
    assert vehicle.longitude == 31.08255
    assert vehicle.gps_actual_time == datetime(2026, 7, 21, 9, 31, 31)
    assert vehicle.vehicle_name == "AFO 1285"
    assert vehicle.speed == 0
    # "--" and "" placeholders normalized to None
    assert vehicle.power is None
    assert vehicle.external_volt is None


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_omits_imei_nos_when_not_given():
    route = respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 49}).respond(
        json={"root": {"VehicleData": []}}
    )

    vehicles = await get_live_data(BASE_URL, "token123", 49, "Chewore")

    assert route.called
    assert b"imei_nos" not in route.calls.last.request.content
    assert vehicles == []


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_omits_company_names_when_not_given():
    # Some accounts map to a single company; omitting the filter returns all
    # vehicles visible to the login, so company_names must be optional.
    route = respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(
        json={"root": {"VehicleData": []}}
    )

    vehicles = await get_live_data(BASE_URL, "token123", 37)

    assert route.called
    assert b"company_names" not in route.calls.last.request.content
    assert vehicles == []


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_no_company_found():
    respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(
        json={"result": 0, "message": "No Company Found"}
    )

    with pytest.raises(TrackitNotFoundException):
        await get_live_data(BASE_URL, "token123", 37, "Unknown Company")


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_unexpected_response():
    respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(
        json={"result": 0, "message": "Something else"}
    )

    with pytest.raises(TrackitBaseException):
        await get_live_data(BASE_URL, "token123", 37, "Chewore")


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_unauthorized():
    respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(status_code=401)

    with pytest.raises(TrackitUnauthorizedException):
        await get_live_data(BASE_URL, "bad-token", 37, "Chewore")


@pytest.mark.asyncio
@respx.mock
async def test_get_live_data_skips_malformed_rows():
    good = VEHICLE_DATA
    bad_missing_imei = {**VEHICLE_DATA, "Imeino": "--"}  # required field normalized to None
    respx.post(BASE_URL, params={"token": "getTokenBaseLiveData", "ProjectId": 37}).respond(
        json={"root": {"VehicleData": [good, bad_missing_imei, "not-a-dict", None]}}
    )

    vehicles = await get_live_data(BASE_URL, "token123", 37, "Chewore")

    # Malformed rows (including non-dict entries) are dropped; the good one survives.
    assert len(vehicles) == 1
    assert vehicles[0].imei == "353742376164273"


def test_vehicle_model_handles_missing_position():
    vehicle = TrackitVehicle.parse_obj({**VEHICLE_DATA, "Latitude": "--", "Longitude": "", "GPSActualTime": "NA"})

    assert vehicle.latitude is None
    assert vehicle.longitude is None
    assert vehicle.gps_actual_time is None


def test_vehicle_model_tolerates_unparseable_gps_time():
    vehicle = TrackitVehicle.parse_obj({**VEHICLE_DATA, "GPSActualTime": "21-07-2026 09:31"})

    # Unparseable time becomes None rather than raising and dropping the row.
    assert vehicle.gps_actual_time is None
    assert vehicle.imei == "353742376164273"
