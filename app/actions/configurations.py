import re

import pydantic

from datetime import timedelta, timezone

from app.actions.core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin
from app.services.errors import ConfigurationNotFound
from app.services.utils import (
    find_config_for_action,
    FieldWithUIOptions,
    UIOptions,
    GlobalUISchemaOptions,
    OptionalStringType,
)


# Per-sign hour bounds (UTC-12 to UTC+14) are encoded in the pattern itself so
# the portal (ajv) rejects out-of-range hours client-side. The validator below
# is still authoritative for the exact range, catching the minute-level edges
# the pattern can't express (e.g. +14:30, -12:30).
# Case-sensitive on purpose: the JSON-schema `pattern` keyword (ajv) doesn't
# carry regex flags, so the backend must accept exactly what the portal does.
# Groups: (positive hours, negative hours, minutes) — exactly one hour group
# matches per input.
UTC_OFFSET_REGEX = re.compile(r"^(?:UTC\s*)?(?:\+?(1[0-4]|0?\d)|-(1[0-2]|0?\d))(?::([0-5]\d))?$")


def utc_offset_to_tzinfo(offset: str) -> timezone:
    """Convert a UTC offset string ('0', '-2', '+3', '+5:30', 'UTC+2') to a tzinfo."""
    match = UTC_OFFSET_REGEX.match(str(offset).strip())
    if not match:
        raise ValueError(f"'{offset}' is not a valid UTC offset. Examples: 0, -2, +3, +5:30")
    pos_hours, neg_hours, minutes = match.groups()
    minutes = int(minutes or 0)
    if neg_hours is not None:
        delta = -timedelta(hours=int(neg_hours), minutes=minutes)
    else:
        delta = timedelta(hours=int(pos_hours), minutes=minutes)
    if not timedelta(hours=-12) <= delta <= timedelta(hours=14):
        raise ValueError(f"UTC offset '{offset}' is out of range (UTC-12 to UTC+14)")
    return timezone(delta)


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    username: str = FieldWithUIOptions(
        ...,
        title="Username",
        description="The username (email) for the TrackIt account. Must be a Company or Company Sub-Account user.",
    )
    password: pydantic.SecretStr = FieldWithUIOptions(
        ...,
        title="Password",
        description="The password for the TrackIt account",
        ui_options=UIOptions(
            widget="password",
        ),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "username",
            "password",
        ],
    )

    class Config:
        title = "Authentication"


class PullObservationsConfig(PullActionConfiguration):
    # Not a destination-only integration type, so the pause toggle is hidden
    run_on_schedule: bool = FieldWithUIOptions(
        True,
        title="Run On Schedule",
        ui_options=UIOptions(
            widget="hidden",
        ),
    )
    company_names: OptionalStringType = FieldWithUIOptions(
        None,
        title="Company Name",
        description="Optional. The TrackIt company to pull vehicle data for. Leave empty to pull all vehicles visible to this account (only one company can be given).",
    )
    imei_nos: OptionalStringType = FieldWithUIOptions(
        None,
        title="IMEI Numbers",
        description="Optional comma-separated list of device IMEIs to pull. Leave empty to pull all vehicles for the company.",
    )
    project_id: int = FieldWithUIOptions(
        37,
        title="Project ID",
        description="The TrackIt project ID, which selects the Trakzee application tier (37 = Premium, 49 = Standard).",
    )
    gps_utc_offset: str = FieldWithUIOptions(
        "+2",
        title="GPS Timestamp UTC Offset",
        description="UTC offset (in hours) of the timestamps reported by the TrackIt platform. Examples: 0, -2, +3, +5:30",
        regex=UTC_OFFSET_REGEX.pattern,
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "company_names",
            "imei_nos",
            "project_id",
            "gps_utc_offset",
            "run_on_schedule",
        ],
    )

    class Config:
        title = "Connection Settings"

    @pydantic.validator("gps_utc_offset")
    def _validate_utc_offset(cls, v):
        utc_offset_to_tzinfo(v)
        return v


def get_auth_config(integration):
    # Look for auth action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)
