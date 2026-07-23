import asyncio
import logging

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import app.actions.client as client

from app.actions.configurations import (
    AuthenticateConfig,
    PullObservationsConfig,
    get_auth_config,
    utc_offset_to_tzinfo,
)
from app.actions.core import action_title
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager
from app.services.utils import generate_batches


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()


TRACKIT_BASE_URL = "https://genx.trackit.co.zw/webservice"
# Must match this handler's registered action id (the "action_" prefix stripped
# from action_pull_observations); it keys the per-device dedup watermarks, so
# changing one without the other silently resets dedup for the whole fleet.
ACTION_ID = "pull_observations"
OBSERVATIONS_BATCH_SIZE = 200
# Positions timestamped further ahead than this are treated as device/config
# clock errors: they are skipped rather than sent or stored, so a single bogus
# future timestamp can't poison the per-device high-watermark permanently.
MAX_FUTURE_SKEW = timedelta(hours=24)
# Cap on concurrent Redis operations, so a large fleet can't open one
# connection per vehicle in a single burst.
MAX_CONCURRENT_STATE_OPS = 25


async def _gather_limited(coros, limit=MAX_CONCURRENT_STATE_OPS):
    """Run coroutines concurrently under a concurrency cap.

    All coroutines run to completion (no orphaned in-flight tasks on failure);
    the first exception, if any, is raised after everything has settled.
    """
    semaphore = asyncio.Semaphore(limit)

    async def run(coro):
        async with semaphore:
            return await coro

    results = await asyncio.gather(*[run(coro) for coro in coros], return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def has_valid_position(vehicle: client.TrackitVehicle) -> bool:
    if vehicle.latitude is None or vehicle.longitude is None or vehicle.gps_actual_time is None:
        return False
    # (0, 0) is TrackIt's null-island for a device without a fix, not a real fix.
    if vehicle.latitude == 0 and vehicle.longitude == 0:
        return False
    return True


def parse_watermark(stored: str) -> Optional[datetime]:
    """Parse a stored watermark into a naive device-local datetime.

    Watermarks are stored as the raw device timestamp (naive), so parsing is a
    plain ISO round-trip. An unparseable value is treated as absent (send +
    overwrite) so one corrupt key can't turn the pull into a permanent crash
    loop.
    """
    try:
        return datetime.fromisoformat(stored)
    except (ValueError, TypeError):
        logger.warning(f"Unparseable stored watermark '{stored}'; treating as absent")
        return None


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


@action_title("Authentication")
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


@action_title("Connection Settings")
@crontab_schedule("*/10 * * * *")
@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    logger.info(f"Executing 'pull_observations' action with integration ID {integration.id} and action_config {action_config}...")

    integration_id = str(integration.id)
    base_url = integration.base_url or TRACKIT_BASE_URL
    auth_config = get_auth_config(integration)
    device_tz = utc_offset_to_tzinfo(action_config.gps_utc_offset)
    now = datetime.now(timezone.utc)

    # Share one connection pool across the token + live-data calls (same host).
    async with httpx.AsyncClient(timeout=client.DEFAULT_TIMEOUT) as session:
        token = await client.get_token(
            base_url, auth_config.username, auth_config.password, session=session
        )
        vehicles = await client.get_live_data(
            base_url,
            token,
            project_id=action_config.project_id,
            company_names=action_config.company_names,
            imei_nos=action_config.imei_nos,
            session=session,
        )
    logger.info(f"-- Extracted {len(vehicles)} vehicles for integration ID: {integration_id} --")

    # Filter invalid rows and collapse duplicate IMEIs (a vehicle can appear
    # more than once in one snapshot) to the row with the newest fix, so two
    # rows can't race the same watermark key or regress it.
    vehicles_without_position = 0
    latest_by_imei = {}
    for vehicle in vehicles:
        if not has_valid_position(vehicle):
            vehicles_without_position += 1
            logger.warning(f"Skipping vehicle {vehicle.imei} (no valid position or GPS time)")
            continue
        current = latest_by_imei.get(vehicle.imei)
        if current is None or vehicle.gps_actual_time > current.gps_actual_time:
            latest_by_imei[vehicle.imei] = vehicle
    unique_vehicles = list(latest_by_imei.values())

    # Prefetch the remaining devices' watermarks concurrently (bounded).
    states = await _gather_limited([
        state_manager.get_state(integration_id=integration_id, action_id=ACTION_ID, source_id=vehicle.imei)
        for vehicle in unique_vehicles
    ])

    new_items = []  # (observation, raw device-time watermark) pairs
    vehicles_future_skewed = 0
    for vehicle, device_state in zip(unique_vehicles, states):
        # Dedup on the raw device timestamp so it is independent of the
        # configured UTC offset — changing the offset can't silently gap data.
        stored = (device_state or {}).get("latest_gps_time")
        if stored:
            watermark = parse_watermark(stored)
            if watermark and vehicle.gps_actual_time <= watermark:
                logger.info(f"Skipping vehicle {vehicle.imei} (no new position since {stored})")
                continue

        recorded_at = vehicle.gps_actual_time.replace(tzinfo=device_tz).astimezone(timezone.utc)
        if recorded_at > now + MAX_FUTURE_SKEW:
            vehicles_future_skewed += 1
            logger.warning(
                f"Skipping vehicle {vehicle.imei}: timestamp {recorded_at.isoformat()} is too far "
                f"in the future (check the GPS Timestamp UTC Offset config)"
            )
            continue

        new_items.append((transform(vehicle, recorded_at), vehicle.gps_actual_time.isoformat()))

    result = {
        "observations_extracted": 0,
        "vehicles_without_position": vehicles_without_position,
        "vehicles_future_skewed": vehicles_future_skewed,
    }
    if not new_items:
        logger.info(f"No new observations to extract for integration ID: {integration_id}")
        return result

    for i, batch in enumerate(generate_batches(new_items, OBSERVATIONS_BATCH_SIZE)):
        logger.info(f"Sending observations batch #{i}: {len(batch)} observations. Integration ID: {integration_id}")
        await send_observations_to_gundi(observations=[obs for obs, _ in batch], integration_id=integration_id)
        result["observations_extracted"] += len(batch)

        # Persist watermarks for this batch as soon as it is delivered, so a
        # later batch failing cannot cause already-sent observations to be
        # re-delivered as duplicates on the next run.
        await _gather_limited([
            state_manager.set_state(
                integration_id=integration_id,
                action_id=ACTION_ID,
                state={"latest_gps_time": watermark},
                source_id=obs["source"],
            )
            for obs, watermark in batch
        ])

    return result
