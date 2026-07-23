import pytest

from app.services.errors import (
    IntegrationError,
    IntegrationAuthError,
    IntegrationConnectionError,
    IntegrationRateLimitError,
    IntegrationBadResponseError,
)


@pytest.mark.parametrize(
    "exception_class,expected_type,expected_title",
    [
        (IntegrationAuthError, "auth", "Authentication failed"),
        (IntegrationConnectionError, "connectivity", "Could not reach the provider"),
        (IntegrationRateLimitError, "rate_limit", "Rate limited by the provider"),
        (IntegrationBadResponseError, "bad_response", "Unexpected response from the provider"),
    ],
)
def test_integration_error_subclasses_define_category(exception_class, expected_type, expected_title):
    exc = exception_class()

    assert isinstance(exc, IntegrationError)
    assert exc.error_type == expected_type
    assert exc.default_title == expected_title
    assert exc.message == expected_title  # defaults to the title when no message given
    assert exc.status_code is None


def test_integration_error_carries_message_and_status_code():
    exc = IntegrationAuthError("TrackIt rejected the credentials", status_code=401)

    assert exc.message == "TrackIt rejected the credentials"
    assert exc.status_code == 401
    assert str(exc) == "TrackIt rejected the credentials"


def test_integration_error_preserves_status_code_set_by_another_base():
    # Client exception hierarchies (e.g. TrackitBaseException) set status_code
    # BEFORE calling super().__init__(message) with no status_code argument.
    # IntegrationError must not clobber it with None.
    class ClientBase(Exception):
        def __init__(self, message, status_code=None):
            self.status_code = status_code
            self.message = message
            super().__init__(message)

    class ClientAuthError(ClientBase, IntegrationAuthError):
        pass

    exc = ClientAuthError("Unauthorized access", status_code=401)

    assert exc.status_code == 401
    assert exc.message == "Unauthorized access"
