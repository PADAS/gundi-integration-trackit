import pydantic

from typing import Optional
from zoneinfo import ZoneInfo

from app.actions.core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action, FieldWithUIOptions, UIOptions, GlobalUISchemaOptions


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
    company_names: str = FieldWithUIOptions(
        ...,
        title="Company Name",
        description="The TrackIt company to pull vehicle data for (a single company per integration)",
    )
    imei_nos: Optional[str] = FieldWithUIOptions(
        None,
        title="IMEI Numbers",
        description="Optional comma-separated list of device IMEIs to pull. Leave empty to pull all vehicles for the company.",
    )
    project_id: int = FieldWithUIOptions(
        37,
        title="Project ID",
        description="The TrackIt project ID (37 = Premium, 49 = Standard)",
    )
    device_timezone: str = FieldWithUIOptions(
        "Africa/Harare",
        title="Device Timezone",
        description="IANA timezone name in which the TrackIt platform reports GPS timestamps (e.g. 'Africa/Harare')",
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "company_names",
            "imei_nos",
            "project_id",
            "device_timezone",
            "run_on_schedule",
        ],
    )

    class Config:
        title = "Pull Observations"

    @pydantic.validator("device_timezone")
    def _validate_timezone(cls, v):
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"'{v}' is not a valid IANA timezone name")
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


def get_pull_config(integration):
    pull_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="pull_observations"
    )
    if not pull_config:
        raise ConfigurationNotFound(
            f"Pull Observations settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return PullObservationsConfig.parse_obj(pull_config.data)
