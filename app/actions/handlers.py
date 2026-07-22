import logging

from datetime import datetime, timezone

import httpx

import app.actions.client as client

from app.actions.configurations import (
    AuthenticateConfig,
    PullObservationsConfig,
    get_auth_config,
    utc_offset_to_tzinfo,
)
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager
from app.services.utils import generate_batches


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()


TRACKIT_BASE_URL = "https://genx.trackit.co.zw/webservice"
OBSERVATIONS_BATCH_SIZE = 200


def transform(vehicle: client.TrackitVehicle, device_tz: timezone) -> dict:
    recorded_at = vehicle.gps_actual_time.replace(tzinfo=device_tz).astimezone(timezone.utc)
    additional = vehicle.dict(
        exclude_none=True,
        exclude={"imei", "latitude", "longitude", "gps_actual_time", "device_datetime"},
    )
    return {
        "source": vehicle.imei,
        "source_name": vehicle.vehicle_name or vehicle.imei,
        "type": "tracking-device",
        "subject_type": "vehicle",
        "recorded_at": recorded_at,
        "location": {
            "lat": vehicle.latitude,
            "lon": vehicle.longitude,
        },
        "additional": additional,
    }


async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(f"Executing 'auth' action with integration ID {integration.id}...")
    base_url = integration.base_url or TRACKIT_BASE_URL

    try:
        token = await client.get_token(base_url, action_config.username, action_config.password)
    except client.TrackitUnauthorizedException as e:
        return {"valid_credentials": False, "status_code": e.status_code, "message": "Bad username and/or password"}
    except client.TrackitInternalServerException as e:
        return {"status": "error", "status_code": e.status_code, "message": "Internal server error at TrackIt"}
    except httpx.HTTPStatusError as e:
        return {"status": "error", "status_code": e.response.status_code, "message": str(e)}

    if token:
        return {"valid_credentials": True}
    return {"valid_credentials": False, "message": "Failed to retrieve token"}


@crontab_schedule("*/10 * * * *")
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    logger.info(f"Executing 'pull_observations' action with integration ID {integration.id} and action_config {action_config}...")

    base_url = integration.base_url or TRACKIT_BASE_URL
    auth_config = get_auth_config(integration)
    device_tz = utc_offset_to_tzinfo(action_config.gps_utc_offset)

    token = await client.get_token(base_url, auth_config.username, auth_config.password)
    vehicles = await client.get_live_data(
        base_url,
        token,
        project_id=action_config.project_id,
        company_names=action_config.company_names,
        imei_nos=action_config.imei_nos,
    )
    logger.info(f"-- Extracted {len(vehicles)} vehicles for integration ID: {integration.id} --")

    transformed_data = []
    latest_times_by_imei = {}
    for vehicle in vehicles:
        if vehicle.latitude is None or vehicle.longitude is None or vehicle.gps_actual_time is None:
            logger.warning(f"Skipping vehicle {vehicle.imei} (no valid position or GPS time)")
            continue

        observation = transform(vehicle, device_tz)

        device_state = await state_manager.get_state(
            integration_id=str(integration.id),
            action_id="pull_observations",
            source_id=vehicle.imei,
        )
        if latest_sent := device_state.get("latest_gps_time") if device_state else None:
            if observation["recorded_at"] <= datetime.fromisoformat(latest_sent):
                logger.info(f"Skipping vehicle {vehicle.imei} (no new position since {latest_sent})")
                continue

        transformed_data.append(observation)
        latest_times_by_imei[vehicle.imei] = observation["recorded_at"]

    if not transformed_data:
        logger.info(f"No new observations to extract for integration ID: {integration.id}")
        return {"observations_extracted": 0}

    observations_extracted = 0
    for i, batch in enumerate(generate_batches(transformed_data, OBSERVATIONS_BATCH_SIZE)):
        logger.info(f"Sending observations batch #{i}: {len(batch)} observations. Integration ID: {integration.id}")
        response = await send_observations_to_gundi(observations=batch, integration_id=str(integration.id))
        observations_extracted += len(response)

    for imei, latest_time in latest_times_by_imei.items():
        await state_manager.set_state(
            integration_id=str(integration.id),
            action_id="pull_observations",
            state={"latest_gps_time": latest_time.isoformat()},
            source_id=imei,
        )

    return {"observations_extracted": observations_extracted}
