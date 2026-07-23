# Consistent Third-Party Error Reporting in Gundi Activity Logs

**Date:** 2026-07-23
**Status:** Approved
**Scope:** Action-runner template layer only (`app/services`, `app/actions`) — no gundi-core or portal changes. Prototyped in this repo with the intent to upstream to `gundi-integration-action-runner`.

## Problem

When an action fails against the third-party system (most commonly an authentication failure), the Activity Log entry in the Gundi portal reads:

> Error running action 'pull_observations': Error in action 'pull_observations' for integration '55f9af92-b426-4f23-a1a1-1a48c2d9a...

The useful information (what actually failed) is buried after a redundant prefix — the action id and integration UUID the portal already displays — and is truncated before it appears. Additionally, a single failure publishes two differently-worded `IntegrationActionFailed` events: one from the `@activity_logger` decorator (`error=str(e)`) and one from `_handle_error` in `app/services/action_runner.py` (the verbose wrapper string).

## Goals

- Auth failures (and other common third-party failure modes) produce short, human-first, consistent error text in the Activity Log.
- Existing integrations built from the template benefit without code changes (heuristic fallback classification).
- New/updated integrations can opt into precise classification by raising standard exceptions.
- Nothing regresses for errors we cannot classify: they keep the current format.

## Non-Goals / Out of Scope

- Changes to gundi-core event models or portal rendering (the portal keeps composing titles as `Error running action '<id>': <error>`).
- Suppressing one of the two duplicate `IntegrationActionFailed` events per failure — that changes portal failure-tracking semantics. Both events will instead carry the same, consistent text.
- Throttling repeated errors. Every failed run publishes its event (decided: broken credentials on a scheduled pull emit an ERROR per tick, matching current behavior).
- Changing the HTTP status codes returned by `execute_action`.
- Changing the skip/invalid-config paths (`_skip_quietly`, `_skip_invalid_config`).

## Design

### 1. Standard exception taxonomy (`app/services/errors.py`)

A small hierarchy that integration clients/handlers raise:

```python
class IntegrationError(Exception):
    """Base for classified third-party failures."""
    error_type = "unknown"          # machine-readable category
    default_title = "Error"         # human-first phrase

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

Constructor accepts an optional human-readable message and an optional `status_code`. The existing exceptions in `errors.py` (`ActionNotFound`, etc.) are untouched.

**TrackIt adoption:** `TrackitUnauthorizedException` in `app/actions/client.py` becomes a subclass of `IntegrationAuthError`, so no call sites change in this repo.

### 2. Classifier with fallback (`classify_error(exc)` in `errors.py`)

Returns the resolved category and title for any exception. Resolution order:

1. **Explicit:** `isinstance(exc, IntegrationError)` → use its `error_type` / `default_title` directly. Explicit always wins.
2. **Heuristic fallback**, from signals `_handle_error` already reads:
   - `exc.response.status_code` 401 or 403 → `auth`
   - `exc.response.status_code` 429 → `rate_limit`
   - `exc.response.status_code` 5xx → `bad_response`
   - `asyncio.TimeoutError`, `aiohttp` client connection errors, `httpx` connect/timeout errors → `connectivity`
3. **Unclassified:** anything else keeps today's generic format (`Error in action '<id>' for integration '<id>': <ExcType>: <exc>`).

### 3. One shared formatter, used by both publication paths

`format_error_message(exc)` produces the clean text for classified errors:

> `Authentication failed — TrackIt rejected the credentials (HTTP 401)`

Format: `<title> — <exception message> (HTTP <status>)`, with the HTTP suffix omitted when no status code is available. The integration UUID and exception class name are dropped from the string — the portal already scopes the log to the integration, and full details remain in the event's `error_traceback`, request, and response fields.

Resulting portal title:

> Error running action 'pull_observations': Authentication failed — TrackIt rejected the credentials (HTTP 401)

Both publication paths call the shared formatter so the two events per failure carry identical text:

- `_handle_error` in `app/services/action_runner.py` — replaces the verbose `message` construction for classified errors.
- The `@activity_logger` decorator in `app/services/activity_logger.py` — replaces the raw `error=str(e)`.

The machine-readable `error_type` string is included in the `error_details` dict that `_handle_error` returns in its JSON response. It cannot ride on the published event itself: `ActionExecutionFailed` is a gundi-core pydantic model that silently drops unknown fields (verified against the installed gundi-core). On the event, the category is conveyed only by the standardized title phrase — adding a first-class `error_type` field to gundi-core is a natural follow-up when upstreaming.

### 4. Testing

- **Classifier unit tests:** each explicit exception type; each heuristic (401, 403, 429, 5xx, timeout, connection refused); unclassified passthrough preserves the current generic format.
- **Publication-path tests:** assert the exact `error` text published in `IntegrationActionFailed` for an auth failure via both `_handle_error` and the `@activity_logger` decorator, and that both are identical.
- **TrackIt integration test:** `TrackitUnauthorizedException` raised by the client surfaces as the auth-classified message.

## Decisions Log

| Decision | Choice |
|---|---|
| Scope | Action-runner template only; no gundi-core/portal changes |
| Error coverage | Small taxonomy: auth, connectivity, rate limit, bad response |
| Surfacing | Clean error text within existing failure events (no CustomActivityLog duplicates) |
| Classification | Explicit standard exceptions, with heuristic fallback for unadopted code |
| Throttling | None — every failed run publishes its event |
