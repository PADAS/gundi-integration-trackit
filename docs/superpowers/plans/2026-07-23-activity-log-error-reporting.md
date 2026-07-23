# Consistent Activity-Log Error Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Third-party failures (auth, connectivity, rate limit, bad response) produce short, human-first, consistent error text in Gundi Activity Log events, with a heuristic fallback so unadopted code benefits too.

**Architecture:** A small exception taxonomy plus a classifier/formatter pair live in `app/services/errors.py`. Both publication paths — `_handle_error` in `app/services/action_runner.py` and the `@activity_logger` decorator in `app/services/activity_logger.py` — format the published `error` string through the shared formatter. The TrackIt client's `TrackitUnauthorizedException` opts in by subclassing `IntegrationAuthError`.

**Tech Stack:** Python 3.10, pydantic v1, httpx, aiohttp, pytest + pytest-asyncio + pytest-mock. Spec: `docs/superpowers/specs/2026-07-23-activity-log-error-reporting-design.md`.

## Global Constraints

- No gundi-core or portal changes. `ActionExecutionFailed` silently drops unknown fields — the machine-readable `error_type` only reaches the JSON response `error_details`, never the event.
- Unclassified errors MUST keep the exact current format: `Error in action '<action_id>' for integration '<integration_id>': <ExcType>: <exc>`.
- Clean text format: `<title> — <message> (HTTP <status>)`; message segment omitted when it equals the title, HTTP suffix omitted when no status code. The separator is an em dash (`—`), not a hyphen.
- Category titles (exact copy): auth → `Authentication failed`; connectivity → `Could not reach the provider`; rate_limit → `Rate limited by the provider`; bad_response → `Unexpected response from the provider`.
- No throttling, no HTTP status-code changes, no changes to skip paths.
- Run tests with: `.venv/bin/python -m pytest <path> -v` from the repo root.

---

### Task 1: Exception taxonomy in `app/services/errors.py`

**Files:**
- Modify: `app/services/errors.py` (append; existing exceptions untouched)
- Test: `app/services/tests/test_errors.py` (create)

**Interfaces:**
- Consumes: nothing (leaf module; must not import from other `app` modules).
- Produces: `IntegrationError` (base, attrs `error_type: str`, `default_title: str`, instance attrs `message: str`, `status_code: Optional[int]`), subclasses `IntegrationAuthError`, `IntegrationConnectionError`, `IntegrationRateLimitError`, `IntegrationBadResponseError`. Constructor signature: `__init__(self, message: str = "", status_code: Optional[int] = None)`. Tasks 2–5 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `app/services/tests/test_errors.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest app/services/tests/test_errors.py -v`
Expected: FAIL with `ImportError: cannot import name 'IntegrationError'`

- [ ] **Step 3: Write the implementation**

Append to `app/services/errors.py` (keep the four existing exception classes above unchanged):

```python
from typing import Optional


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
```

Note: the `from typing import Optional` import goes at the top of the file, not mid-file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest app/services/tests/test_errors.py -v`
Expected: PASS (4 tests: 1 parametrized x4 counts as 4, plus 2 more = 6 test items)

- [ ] **Step 5: Commit**

```bash
git add app/services/errors.py app/services/tests/test_errors.py
git commit -m "feat: add standard integration error taxonomy"
```

---

### Task 2: Classifier and formatter in `app/services/errors.py`

**Files:**
- Modify: `app/services/errors.py` (append below Task 1's classes)
- Test: `app/services/tests/test_errors.py` (append)

**Interfaces:**
- Consumes: Task 1's exception classes.
- Produces:
  - `ClassifiedError` — `NamedTuple(error_type: str, title: str, message: str, status_code: Optional[int])`
  - `classify_error(exc: Exception) -> Optional[ClassifiedError]` — None means unclassified.
  - `format_classified_error(classified: ClassifiedError) -> str` — clean text from an already-classified error.
  - `format_error_message(exc: Exception) -> Optional[str]` — classify + format in one call; None means unclassified.
  - Tasks 3–5 rely on these exact names and signatures.

- [ ] **Step 1: Write the failing tests**

Append to `app/services/tests/test_errors.py`:

```python
import asyncio

import aiohttp
import httpx

from app.services.errors import classify_error, format_error_message


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.com/data")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def test_classify_explicit_integration_error_wins_over_heuristics():
    exc = IntegrationBadResponseError("Provider returned XML instead of JSON", status_code=401)

    classified = classify_error(exc)

    # 401 would heuristically be auth, but the explicit type wins
    assert classified.error_type == "bad_response"
    assert classified.title == "Unexpected response from the provider"
    assert classified.message == "Provider returned XML instead of JSON"
    assert classified.status_code == 401


@pytest.mark.parametrize(
    "status_code,expected_type,expected_title",
    [
        (401, "auth", "Authentication failed"),
        (403, "auth", "Authentication failed"),
        (429, "rate_limit", "Rate limited by the provider"),
        (500, "bad_response", "Unexpected response from the provider"),
        (503, "bad_response", "Unexpected response from the provider"),
    ],
)
def test_classify_by_response_status_code(status_code, expected_type, expected_title):
    classified = classify_error(_http_status_error(status_code))

    assert classified.error_type == expected_type
    assert classified.title == expected_title
    assert classified.status_code == status_code


@pytest.mark.parametrize(
    "exc",
    [
        asyncio.TimeoutError("timed out"),
        ConnectionRefusedError("connection refused"),
        httpx.ConnectError("connection failed"),
        httpx.ReadTimeout("read timed out"),
        aiohttp.ClientConnectionError("cannot connect"),
    ],
)
def test_classify_connectivity_errors(exc):
    classified = classify_error(exc)

    assert classified.error_type == "connectivity"
    assert classified.title == "Could not reach the provider"
    assert classified.status_code is None


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("boom"),
        KeyError("missing"),
        _http_status_error(400),  # 4xx other than 401/403/429 stays unclassified
    ],
)
def test_classify_returns_none_for_unclassified_errors(exc):
    assert classify_error(exc) is None
    assert format_error_message(exc) is None


def test_format_full_message_with_status():
    exc = IntegrationAuthError("TrackIt rejected the credentials", status_code=401)

    assert format_error_message(exc) == "Authentication failed — TrackIt rejected the credentials (HTTP 401)"


def test_format_omits_message_segment_when_it_equals_the_title():
    exc = IntegrationAuthError(status_code=401)

    assert format_error_message(exc) == "Authentication failed (HTTP 401)"


def test_format_omits_http_suffix_without_status_code():
    exc = IntegrationConnectionError("DNS lookup failed")

    assert format_error_message(exc) == "Could not reach the provider — DNS lookup failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest app/services/tests/test_errors.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify_error'`

- [ ] **Step 3: Write the implementation**

Append to `app/services/errors.py`, and move all imports to the top of the file so it starts with:

```python
import asyncio
from typing import NamedTuple, Optional

import aiohttp
import httpx
```

Then append after the exception classes:

```python
class ClassifiedError(NamedTuple):
    error_type: str
    title: str
    message: str
    status_code: Optional[int]


# Exceptions that mean the provider could not be reached at all.
CONNECTIVITY_EXCEPTIONS = (
    asyncio.TimeoutError,
    ConnectionError,  # builtin: covers ConnectionRefusedError, ConnectionResetError, etc.
    httpx.TransportError,  # covers ConnectError, ReadTimeout, and all transport failures
    aiohttp.ClientConnectionError,
)


def classify_error(exc: Exception) -> Optional[ClassifiedError]:
    """Classify a third-party failure for consistent activity-log reporting.

    Explicitly raised `IntegrationError` subclasses always win. Otherwise fall
    back to heuristics based on signals the action runner already reads
    (`exc.response.status_code`, exception type). Returns None when the error
    can't be classified — callers keep the generic format.
    """
    if isinstance(exc, IntegrationError):
        return ClassifiedError(
            error_type=exc.error_type,
            title=exc.default_title,
            message=getattr(exc, "message", None) or "",
            status_code=getattr(exc, "status_code", None),
        )

    # Use getattr (not truthiness): httpx error responses are falsy.
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in (401, 403):
        return ClassifiedError("auth", IntegrationAuthError.default_title, str(exc), status_code)
    if status_code == 429:
        return ClassifiedError("rate_limit", IntegrationRateLimitError.default_title, str(exc), status_code)
    if status_code is not None and status_code >= 500:
        return ClassifiedError("bad_response", IntegrationBadResponseError.default_title, str(exc), status_code)
    if isinstance(exc, CONNECTIVITY_EXCEPTIONS):
        return ClassifiedError("connectivity", IntegrationConnectionError.default_title, str(exc), None)
    return None


def format_classified_error(classified: ClassifiedError) -> str:
    """Build the clean text: "<title> — <message> (HTTP <status>)".

    The portal prepends "Error running action '<id>': " to this string, so it
    must be short and lead with what an operator needs to see. The message
    segment is skipped when redundant; the HTTP suffix when unknown.
    """
    text = classified.title
    if classified.message and classified.message != classified.title:
        text = f"{text} — {classified.message}"
    if classified.status_code:
        text = f"{text} (HTTP {classified.status_code})"
    return text


def format_error_message(exc: Exception) -> Optional[str]:
    """Return clean, human-first error text for classified errors, or None."""
    classified = classify_error(exc)
    return format_classified_error(classified) if classified else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest app/services/tests/test_errors.py -v`
Expected: PASS (all test items, including Task 1's)

- [ ] **Step 5: Commit**

```bash
git add app/services/errors.py app/services/tests/test_errors.py
git commit -m "feat: add error classifier and clean-text formatter"
```

---

### Task 3: `_handle_error` publishes classified text

**Files:**
- Modify: `app/services/action_runner.py:56-101` (`_handle_error`)
- Test: `app/services/tests/test_action_runner.py` (append)

**Interfaces:**
- Consumes: `classify_error`, `format_classified_error` from `app.services.errors` (Task 2).
- Produces: no new interfaces. Behavior change: for classified errors, the published `IntegrationActionFailed.payload.error` and the JSON response's `error_details["error"]` carry the clean text, and `error_details["error_type"]` carries the category (JSON response only — pydantic drops it from the event).

- [ ] **Step 1: Write the failing test**

Append to `app/services/tests/test_action_runner.py`. The file already imports `json` and `IntegrationActionFailed` and defines the `_published_events_of_type` helper at the top, but it does NOT import `execute_action` (existing tests go through the API test client) — add both imports:

```python
from app.services.action_runner import execute_action
from app.services.errors import IntegrationAuthError


@pytest.mark.asyncio
async def test_execute_action_reports_classified_auth_error_with_clean_text(
        mocker, mock_gundi_client_v2, integration_v2, mock_config_manager,
        mock_publish_event, mock_action_handlers,
):
    mock_handler, _, _ = mock_action_handlers["pull_observations"]
    mock_handler.side_effect = IntegrationAuthError("TrackIt rejected the credentials", status_code=401)
    mocker.patch("app.services.action_runner.action_handlers", mock_action_handlers)
    mocker.patch("app.services.action_runner._portal", mock_gundi_client_v2)
    mocker.patch("app.services.action_runner.config_manager", mock_config_manager)
    mocker.patch("app.services.activity_logger.publish_event", mock_publish_event)
    mocker.patch("app.services.action_runner.publish_event", mock_publish_event)

    response = await execute_action(
        integration_id=str(integration_v2.id),
        action_id="pull_observations",
    )

    expected_text = "Authentication failed — TrackIt rejected the credentials (HTTP 401)"
    failed_events = _published_events_of_type(mock_publish_event, IntegrationActionFailed)
    assert len(failed_events) >= 1
    for event in failed_events:
        assert event.payload.error == expected_text
    error_details = json.loads(response.body)["detail"]
    assert error_details["error"] == expected_text
    assert error_details["error_type"] == "auth"


@pytest.mark.asyncio
async def test_execute_action_keeps_generic_format_for_unclassified_errors(
        mocker, mock_gundi_client_v2, integration_v2, mock_config_manager,
        mock_publish_event, mock_action_handlers,
):
    mock_handler, _, _ = mock_action_handlers["pull_observations"]
    mock_handler.side_effect = ValueError("something unexpected")
    mocker.patch("app.services.action_runner.action_handlers", mock_action_handlers)
    mocker.patch("app.services.action_runner._portal", mock_gundi_client_v2)
    mocker.patch("app.services.action_runner.config_manager", mock_config_manager)
    mocker.patch("app.services.activity_logger.publish_event", mock_publish_event)
    mocker.patch("app.services.action_runner.publish_event", mock_publish_event)

    response = await execute_action(
        integration_id=str(integration_v2.id),
        action_id="pull_observations",
    )

    error_details = json.loads(response.body)["detail"]
    assert error_details["error"] == (
        f"Error in action 'pull_observations' for integration '{str(integration_v2.id)}': "
        f"ValueError: something unexpected"
    )
    assert error_details["error_type"] is None
```

Note: `mock_action_handlers` handler mocks are `AsyncMock`s, so setting `side_effect` on the shared fixture instance is safe per-test (fixtures are function-scoped).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest app/services/tests/test_action_runner.py -v -k "classified or unclassified"`
Expected: FAIL — `test_execute_action_reports_classified_auth_error_with_clean_text` gets the generic message instead of the clean text; the unclassified test fails on the missing `error_type` key.

- [ ] **Step 3: Write the implementation**

In `app/services/action_runner.py`, add to the imports near the top (alongside the other `from .` imports):

```python
from .errors import classify_error, format_classified_error
```

Then in `_handle_error`, replace:

```python
    message = f"Error in action '{action_id}' for integration '{integration_id}': {type(exc).__name__}: {exc}"
    logger.exception(message)

    error_details = {
        "integration_id": integration_id,
        "action_id": action_id,
        "config_data": config_data or {},
        "error": message,
        "error_traceback": traceback.format_exc()
    }
```

with:

```python
    # Classified errors (auth, connectivity, rate limit, bad response) get
    # short human-first text — the portal prepends "Error running action
    # '<id>': " and truncates, so the useful part must come first. Anything
    # unclassified keeps the verbose format. Full details always remain in
    # error_traceback and the request/response fields below.
    classified = classify_error(exc)
    if classified:
        message = format_classified_error(classified)
    else:
        message = f"Error in action '{action_id}' for integration '{integration_id}': {type(exc).__name__}: {exc}"
    logger.exception(message)

    error_details = {
        "integration_id": integration_id,
        "action_id": action_id,
        "config_data": config_data or {},
        "error": message,
        # Machine-readable category. Only reaches the JSON response below;
        # ActionExecutionFailed is a gundi-core model that drops unknown fields.
        "error_type": classified.error_type if classified else None,
        "error_traceback": traceback.format_exc()
    }
```

- [ ] **Step 4: Run the full action-runner test suite to verify nothing regressed**

Run: `.venv/bin/python -m pytest app/services/tests/test_action_runner.py -v`
Expected: PASS — including the pre-existing `mock_action_handlers_with_request_errors` tests (they assert `payload.error` is truthy, not its exact text; the 500-error case now gets classified text, which is still truthy).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_runner.py app/services/tests/test_action_runner.py
git commit -m "feat: publish classified clean error text from _handle_error"
```

---

### Task 4: `@activity_logger` decorator publishes the same text

**Files:**
- Modify: `app/services/activity_logger.py:157` (the `error=str(e)` line in the `activity_logger` decorator)
- Test: `app/services/tests/test_activity_logger.py` (append)

**Interfaces:**
- Consumes: `format_error_message` from `app.services.errors` (Task 2).
- Produces: no new interfaces. Behavior change: the decorator's `IntegrationActionFailed.payload.error` carries the same clean text as `_handle_error`'s event for classified errors, `str(e)` otherwise.

- [ ] **Step 1: Write the failing test**

Append to `app/services/tests/test_activity_logger.py`:

```python
from app.services.errors import IntegrationAuthError


@pytest.mark.asyncio
async def test_activity_logger_decorator_publishes_classified_error_text(
        mocker, mock_publish_event, integration_v2, pull_observations_config
):
    mocker.patch("app.services.activity_logger.publish_event", mock_publish_event)

    @activity_logger()
    async def action_pull_observations(integration, action_config):
        raise IntegrationAuthError("TrackIt rejected the credentials", status_code=401)

    with pytest.raises(IntegrationAuthError):
        await action_pull_observations(
            integration=integration_v2, action_config=pull_observations_config
        )

    failed_events = [
        call.kwargs.get("event") or call.args[0]
        for call in mock_publish_event.mock_calls
        if call.kwargs.get("event") is not None or call.args
    ]
    failed_events = [e for e in failed_events if isinstance(e, IntegrationActionFailed)]
    assert len(failed_events) == 1
    assert failed_events[0].payload.error == (
        "Authentication failed — TrackIt rejected the credentials (HTTP 401)"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest app/services/tests/test_activity_logger.py -v -k classified`
Expected: FAIL — `payload.error` is `str(e)` (`"TrackIt rejected the credentials"`), not the clean formatted text.

- [ ] **Step 3: Write the implementation**

In `app/services/activity_logger.py`, add to the imports:

```python
from app.services.errors import format_error_message
```

In the `activity_logger` decorator's `except` block, replace:

```python
                                error=str(e)
```

with:

```python
                                error=format_error_message(e) or str(e)
```

(Only in `activity_logger`, not `webhook_activity_logger` — webhooks are out of this design's scope.)

- [ ] **Step 4: Run the full activity-logger test suite**

Run: `.venv/bin/python -m pytest app/services/tests/test_activity_logger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/activity_logger.py app/services/tests/test_activity_logger.py
git commit -m "feat: publish classified error text from activity_logger decorator"
```

---

### Task 5: TrackIt client adoption

**Files:**
- Modify: `app/actions/client.py:53-54` (`TrackitUnauthorizedException`)
- Test: `app/actions/tests/test_client.py` (append)

**Interfaces:**
- Consumes: `IntegrationAuthError` from `app.services.errors` (Task 1), `format_error_message` (Task 2, test only).
- Produces: `TrackitUnauthorizedException` is now also an `IntegrationAuthError`. No call-site changes: `TrackitBaseException.__init__` still runs first in the MRO and its attribute setup is preserved (verified by Task 1's cooperative-`__init__` test).

- [ ] **Step 1: Write the failing test**

Append to `app/actions/tests/test_client.py`:

```python
from app.services.errors import IntegrationAuthError, format_error_message


def test_trackit_unauthorized_exception_is_classified_as_auth_error():
    exc = TrackitUnauthorizedException("Unauthorized access", ValueError("original"))

    assert isinstance(exc, IntegrationAuthError)
    assert exc.status_code == 401  # default_status_code preserved through the MRO
    assert format_error_message(exc) == "Authentication failed — Unauthorized access (HTTP 401)"


def test_trackit_unauthorized_exception_keeps_explicit_status_code():
    exc = TrackitUnauthorizedException("Forbidden", None, status_code=403)

    assert exc.status_code == 403
    assert format_error_message(exc) == "Authentication failed — Forbidden (HTTP 403)"
```

`TrackitUnauthorizedException` is already imported in `test_client.py`; if not, add it to the existing `from app.actions.client import ...` line.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest app/actions/tests/test_client.py -v -k unauthorized`
Expected: FAIL with `assert isinstance(exc, IntegrationAuthError)` being False (plus any pre-existing tests matching `-k unauthorized` still passing).

- [ ] **Step 3: Write the implementation**

In `app/actions/client.py`, add to the imports:

```python
from app.services.errors import IntegrationAuthError
```

(`app/services/__init__.py` is empty, so this creates no import cycle.)

Change the class definition from:

```python
class TrackitUnauthorizedException(TrackitBaseException):
    default_status_code = 401
```

to:

```python
class TrackitUnauthorizedException(TrackitBaseException, IntegrationAuthError):
    """Also an IntegrationAuthError so activity logs report it as
    "Authentication failed — ..." instead of the generic error format."""
    default_status_code = 401
```

- [ ] **Step 4: Run the full client and handler test suites**

Run: `.venv/bin/python -m pytest app/actions/tests/ -v`
Expected: PASS — existing `handle_httpx_error` tests still see `TrackitUnauthorizedException` with the same `str()` output (`TrackitBaseException.__str__` is earliest in the MRO and unchanged).

- [ ] **Step 5: Run the entire test suite and commit**

Run: `.venv/bin/python -m pytest app -v`
Expected: PASS (full suite green)

```bash
git add app/actions/client.py app/actions/tests/test_client.py
git commit -m "feat: classify TrackIt auth failures as IntegrationAuthError"
```
