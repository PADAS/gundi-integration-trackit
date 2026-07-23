from typing import Optional


class ActionNotFound(Exception):
    pass


class ConfigurationNotFound(Exception):
    pass


class ConfigurationValidationError(Exception):
    pass


class ActionExecutionError(Exception):
    pass


class IntegrationError(Exception):
    """Base for classified third-party failures.

    Subclasses set `error_type` (machine-readable category) and
    `default_title` (human-first phrase shown in the portal activity log).

    Cooperates with client exception hierarchies (e.g. a connector's own
    base exception) that set `message`/`status_code` before calling
    super().__init__(message): a status_code already set by an earlier
    __init__ in the MRO is never clobbered with None.
    """
    error_type = "unknown"
    default_title = "Error"

    def __init__(self, message: str = "", status_code: Optional[int] = None):
        super().__init__(message or self.default_title)
        self.message = message or self.default_title
        if status_code is not None:
            self.status_code = status_code
        elif not hasattr(self, "status_code"):
            self.status_code = None


class IntegrationAuthError(IntegrationError):
    error_type = "auth"
    default_title = "Authentication failed"


class IntegrationConnectionError(IntegrationError):
    error_type = "connectivity"
    default_title = "Could not reach the provider"


class IntegrationRateLimitError(IntegrationError):
    error_type = "rate_limit"
    default_title = "Rate limited by the provider"


class IntegrationBadResponseError(IntegrationError):
    error_type = "bad_response"
    default_title = "Unexpected response from the provider"
