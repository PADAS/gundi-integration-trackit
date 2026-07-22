import asyncio
import logging

from datetime import datetime, timedelta, timezone

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
# Positions timestamped further ahead than this are treated as device/config
# clock errors: they are skipped rather than sent or stored, so a single bogus
# future timestamp can't poison the per-device high-watermark permanently.
MAX_FUTURE_SKEW = timedelta(hours=24)


def has_valid_position(vehicle: client.TrackitVehicle) -> bool:
    if vehicle.latitude is None or vehicle.longitude is None or vehicle.gps_actual_time is None:
        return False
    # (0, 0) is TrackIt's null-island for a device without a fix, not a real fix.
    if vehicle.latitude == 0 and vehicle.longitude == 0:
        return False
    return True


def parse_watermark(stored: str) -> datetime:
    """Parse a stored watermark to a naive datetime for offset-independent comparison.

    Watermarks are stored as the raw device timestamp (naive). Values written by
    an earlier version were UTC-aware; drop the tzinfo so the comparison never
    mixes naive and aware datetimes (which would raise).
    """
    dt = datetime.fromisoformat(stored)
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def transform(vehicle: client.TrackitVehicle, recorded_at: datetime) -> dict:
    additional = vehicle.dict(
        exclude_none=True,
        exclude={"imei", "latitude", "longitude", "gps_actual_time"},
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
        await client.get_token(base_url, action_config.username, action_config.password)
    except client.TrackitUnauthorizedException as e:
        return {"valid_credentials": False, "status_code": e.status_code, "message": "Bad username and/or password"}
    except client.TrackitBaseException as e:
        # 404, 5xx, non-JSON body, or any other API-level failure.
        return {"valid_credentials": False, "status_code": e.status_code, "message": str(e.message)}
    except httpx.HTTPError as e:
        # Transport-level failures (DNS, connect/read timeout, etc.).
        return {"valid_credentials": False, "message": f"Could not reach TrackIt: {e}"}

    return {"valid_credentials": True}


@crontab_schedule("*/10 * * * *")
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    logger.info(f"Executing 'pull_observations' action with integration ID {integration.id} and action_config {action_config}...")

    integration_id = str(integration.id)
    base_url = integration.base_url or TRACKIT_BASE_URL
    auth_config = get_auth_config(integration)
    device_tz = utc_offset_to_tzinfo(action_config.gps_utc_offset)
    now = datetime.now(timezone.utc)

    token = await client.get_token(base_url, auth_config.username, auth_config.password)
    vehicles = await client.get_live_data(
        base_url,
        token,
        project_id=action_config.project_id,
        company_names=action_config.company_names,
        imei_nos=action_config.imei_nos,
    )
    logger.info(f"-- Extracted {len(vehicles)} vehicles for integration ID: {integration_id} --")

    # Prefetch every device's watermark in one concurrent batch instead of a
    # sequential Redis round-trip per vehicle.
    imeis = [vehicle.imei for vehicle in vehicles]
    states = await asyncio.gather(*[
        state_manager.get_state(integration_id=integration_id, action_id="pull_observations", source_id=imei)
        for imei in imeis
    ])
    state_by_imei = dict(zip(imeis, states))

    observations = []
    raw_time_by_imei = {}
    vehicles_skipped = 0
    for vehicle in vehicles:
        if not has_valid_position(vehicle):
            vehicles_skipped += 1
            logger.warning(f"Skipping vehicle {vehicle.imei} (no valid position or GPS time)")
            continue

        # Dedup on the raw device timestamp so it is independent of the
        # configured UTC offset — changing the offset can't silently gap data.
        stored = (state_by_imei.get(vehicle.imei) or {}).get("latest_gps_time")
        if stored and vehicle.gps_actual_time <= parse_watermark(stored):
            logger.info(f"Skipping vehicle {vehicle.imei} (no new position since {stored})")
            continue

        recorded_at = vehicle.gps_actual_time.replace(tzinfo=device_tz).astimezone(timezone.utc)
        if recorded_at > now + MAX_FUTURE_SKEW:
            vehicles_skipped += 1
            logger.warning(
                f"Skipping vehicle {vehicle.imei}: timestamp {recorded_at.isoformat()} is too far "
                f"in the future (check the GPS Timestamp UTC Offset config)"
            )
            continue

        observations.append(transform(vehicle, recorded_at))
        raw_time_by_imei[vehicle.imei] = vehicle.gps_actual_time.isoformat()

    if not observations:
        logger.info(f"No new observations to extract for integration ID: {integration_id}")
        return {"observations_extracted": 0, "vehicles_skipped": vehicles_skipped}

    observations_extracted = 0
    for i, batch in enumerate(generate_batches(observations, OBSERVATIONS_BATCH_SIZE)):
        logger.info(f"Sending observations batch #{i}: {len(batch)} observations. Integration ID: {integration_id}")
        await send_observations_to_gundi(observations=batch, integration_id=integration_id)
        observations_extracted += len(batch)

        # Persist watermarks for this batch as soon as it is delivered, so a
        # later batch failing cannot cause already-sent observations to be
        # re-delivered as duplicates on the next run.
        await asyncio.gather(*[
            state_manager.set_state(
                integration_id=integration_id,
                action_id="pull_observations",
                state={"latest_gps_time": raw_time_by_imei[obs["source"]]},
                source_id=obs["source"],
            )
            for obs in batch
        ])

    return {"observations_extracted": observations_extracted, "vehicles_skipped": vehicles_skipped}
