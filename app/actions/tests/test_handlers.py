import httpx
import pytest
import pydantic

from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

import app.actions.handlers as handlers
import app.actions.client as client

from app.actions.configurations import AuthenticateConfig, PullObservationsConfig, utc_offset_to_tzinfo


VEHICLE = client.TrackitVehicle(
    Imeino="353742376164273",
    Latitude="-17.8103899",
    Longitude="31.08255",
    GPSActualTime="21-07-2026 09:31:31",
    Vehicle_Name="AFO 1285",
    Vehicle_No="AFO 1285",
    Vehicletype="Car",
    Speed="0",
    Angle="76",
    Altitude="1492",
    Status="STOP",
    IGN="OFF",
    Company="Chewore",
)


@pytest.fixture
def mock_integration():
    integration = MagicMock()
    integration.id = "integration_id"
    integration.base_url = None
    return integration


@pytest.fixture
def pull_config():
    return PullObservationsConfig(company_names="Chewore", imei_nos="353742376164273")


@pytest.fixture
def auth_config():
    return AuthenticateConfig(username="user@test.org", password="secret")


@pytest.fixture
def mock_pull_dependencies(mocker, auth_config):
    mocker.patch("app.actions.handlers.get_auth_config", return_value=auth_config)
    mocker.patch("app.actions.client.get_token", new_callable=AsyncMock, return_value="token123")
    mocks = {
        "get_live_data": mocker.patch(
            "app.actions.client.get_live_data", new_callable=AsyncMock, return_value=[VEHICLE]
        ),
        "get_state": mocker.patch(
            "app.actions.handlers.state_manager.get_state", new_callable=AsyncMock, return_value={}
        ),
        "set_state": mocker.patch(
            "app.actions.handlers.state_manager.set_state", new_callable=AsyncMock
        ),
        "send_observations": mocker.patch(
            "app.actions.handlers.send_observations_to_gundi",
            new_callable=AsyncMock,
            side_effect=lambda observations, **kwargs: observations,
        ),
        "publish_event": mocker.patch(
            "app.services.activity_logger.publish_event", new_callable=AsyncMock
        ),
    }
    return mocks


def test_utc_offset_to_tzinfo():
    assert utc_offset_to_tzinfo("+2") == timezone(timedelta(hours=2))
    assert utc_offset_to_tzinfo("2") == timezone(timedelta(hours=2))
    assert utc_offset_to_tzinfo("0") == timezone.utc
    assert utc_offset_to_tzinfo("-3:30") == timezone(timedelta(hours=-3, minutes=-30))
    assert utc_offset_to_tzinfo("+5:30") == timezone(timedelta(hours=5, minutes=30))
    assert utc_offset_to_tzinfo("UTC+2") == timezone(timedelta(hours=2))
    for invalid in ["abc", "+15", "-13", "+2:75", ""]:
        with pytest.raises(ValueError):
            utc_offset_to_tzinfo(invalid)


def test_pull_config_rejects_invalid_offset():
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfig(company_names="Chewore", gps_utc_offset="whatever")


def test_transform():
    recorded_at = datetime(2026, 7, 21, 7, 31, 31, tzinfo=timezone.utc)
    observation = handlers.transform(VEHICLE, recorded_at)

    assert observation["source"] == "353742376164273"
    assert observation["source_name"] == "AFO 1285"
    assert observation["type"] == "tracking-device"
    assert observation["subject_type"] == "vehicle"
    assert observation["recorded_at"] == recorded_at
    assert observation["location"] == {"lat": -17.8103899, "lon": 31.08255}
    assert observation["additional"]["vehicle_no"] == "AFO 1285"
    assert observation["additional"]["speed"] == 0
    assert "gps_actual_time" not in observation["additional"]


def test_has_valid_position():
    assert handlers.has_valid_position(VEHICLE) is True
    assert handlers.has_valid_position(VEHICLE.copy(update={"latitude": None})) is False
    assert handlers.has_valid_position(VEHICLE.copy(update={"gps_actual_time": None})) is False
    # null island (0, 0)
    assert handlers.has_valid_position(VEHICLE.copy(update={"latitude": 0.0, "longitude": 0.0})) is False


def test_parse_watermark_handles_naive_and_aware():
    assert handlers.parse_watermark("2026-07-21T09:31:31") == datetime(2026, 7, 21, 9, 31, 31)
    # aware values written by an earlier version are coerced to naive
    assert handlers.parse_watermark("2026-07-21T09:31:31+00:00") == datetime(2026, 7, 21, 9, 31, 31)


@pytest.mark.asyncio
async def test_action_auth_success(mocker, mock_integration, auth_config):
    mock_get_token = mocker.patch(
        "app.actions.client.get_token", new_callable=AsyncMock, return_value="token123"
    )

    result = await handlers.action_auth(mock_integration, auth_config)

    mock_get_token.assert_awaited_once_with(
        handlers.TRACKIT_BASE_URL, auth_config.username, auth_config.password
    )
    assert result == {"valid_credentials": True}


@pytest.mark.asyncio
async def test_action_auth_bad_credentials(mocker, mock_integration, auth_config):
    mocker.patch(
        "app.actions.client.get_token",
        new_callable=AsyncMock,
        side_effect=client.TrackitUnauthorizedException("Login failed"),
    )

    result = await handlers.action_auth(mock_integration, auth_config)

    assert result["valid_credentials"] is False
    assert result["status_code"] == 401


@pytest.mark.asyncio
async def test_action_auth_not_found_returns_result_dict(mocker, mock_integration, auth_config):
    mocker.patch(
        "app.actions.client.get_token",
        new_callable=AsyncMock,
        side_effect=client.TrackitNotFoundException("Not found"),
    )

    result = await handlers.action_auth(mock_integration, auth_config)

    assert result["valid_credentials"] is False
    assert result["status_code"] == 404


@pytest.mark.asyncio
async def test_action_auth_transport_error_returns_result_dict(mocker, mock_integration, auth_config):
    mocker.patch(
        "app.actions.client.get_token",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectError("name resolution failed"),
    )

    result = await handlers.action_auth(mock_integration, auth_config)

    assert result["valid_credentials"] is False
    assert "Could not reach TrackIt" in result["message"]


@pytest.mark.asyncio
async def test_action_pull_observations(mock_integration, pull_config, mock_pull_dependencies):
    result = await handlers.action_pull_observations(mock_integration, pull_config)

    mock_pull_dependencies["get_live_data"].assert_awaited_once_with(
        handlers.TRACKIT_BASE_URL,
        "token123",
        project_id=37,
        company_names="Chewore",
        imei_nos="353742376164273",
    )
    mock_pull_dependencies["send_observations"].assert_awaited_once()
    # Watermark is the raw (naive) device timestamp, independent of the offset.
    mock_pull_dependencies["set_state"].assert_awaited_once_with(
        integration_id="integration_id",
        action_id="pull_observations",
        state={"latest_gps_time": "2026-07-21T09:31:31"},
        source_id="353742376164273",
    )
    assert result == {"observations_extracted": 1, "vehicles_skipped": 0}


@pytest.mark.asyncio
async def test_action_pull_observations_skips_already_sent_positions(
        mock_integration, pull_config, mock_pull_dependencies
):
    mock_pull_dependencies["get_state"].return_value = {"latest_gps_time": "2026-07-21T09:31:31"}

    result = await handlers.action_pull_observations(mock_integration, pull_config)

    mock_pull_dependencies["send_observations"].assert_not_awaited()
    mock_pull_dependencies["set_state"].assert_not_awaited()
    assert result == {"observations_extracted": 0, "vehicles_skipped": 0}


@pytest.mark.asyncio
async def test_action_pull_observations_dedup_is_offset_independent(
        mocker, mock_integration, mock_pull_dependencies
):
    # Watermark stored while running at +2; operator later switches to 0.
    mock_pull_dependencies["get_state"].return_value = {"latest_gps_time": "2026-07-21T09:31:31"}
    config = PullObservationsConfig(company_names="Chewore", gps_utc_offset="0")

    result = await handlers.action_pull_observations(mock_integration, config)

    # Same raw device time as the watermark -> still deduped despite the offset change.
    mock_pull_dependencies["send_observations"].assert_not_awaited()
    assert result["observations_extracted"] == 0


@pytest.mark.asyncio
async def test_action_pull_observations_skips_vehicles_without_position(
        mock_integration, pull_config, mock_pull_dependencies
):
    vehicle_without_position = VEHICLE.copy(update={"latitude": None, "longitude": None})
    mock_pull_dependencies["get_live_data"].return_value = [vehicle_without_position]

    result = await handlers.action_pull_observations(mock_integration, pull_config)

    mock_pull_dependencies["send_observations"].assert_not_awaited()
    assert result == {"observations_extracted": 0, "vehicles_skipped": 1}


@pytest.mark.asyncio
async def test_action_pull_observations_skips_future_timestamps(
        mock_integration, pull_config, mock_pull_dependencies
):
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)
    future_vehicle = VEHICLE.copy(update={"gps_actual_time": future})
    mock_pull_dependencies["get_live_data"].return_value = [future_vehicle]

    result = await handlers.action_pull_observations(mock_integration, pull_config)

    mock_pull_dependencies["send_observations"].assert_not_awaited()
    mock_pull_dependencies["set_state"].assert_not_awaited()
    assert result == {"observations_extracted": 0, "vehicles_skipped": 1}


@pytest.mark.asyncio
async def test_action_pull_observations_saves_state_per_batch(
        mocker, mock_integration, pull_config, mock_pull_dependencies
):
    # Two vehicles, batch size 1 -> two batches. Second send fails; the first
    # batch's watermark must already be persisted so it is not re-sent next run.
    vehicle_b = VEHICLE.copy(update={"imei": "999", "gps_actual_time": datetime(2026, 7, 21, 10, 0, 0)})
    mock_pull_dependencies["get_live_data"].return_value = [VEHICLE, vehicle_b]
    mocker.patch("app.actions.handlers.OBSERVATIONS_BATCH_SIZE", 1)
    mock_pull_dependencies["send_observations"].side_effect = [["ok"], httpx.HTTPError("boom")]

    with pytest.raises(httpx.HTTPError):
        await handlers.action_pull_observations(mock_integration, pull_config)

    # State saved for the first (delivered) batch only.
    mock_pull_dependencies["set_state"].assert_awaited_once_with(
        integration_id="integration_id",
        action_id="pull_observations",
        state={"latest_gps_time": "2026-07-21T09:31:31"},
        source_id="353742376164273",
    )
