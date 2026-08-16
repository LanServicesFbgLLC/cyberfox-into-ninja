"""Shared HTTP behaviour: retries with backoff, and Retry-After awareness."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _sleep_for(attempt: int, response: Optional[httpx.Response]) -> float:
    """Honour Retry-After when present, otherwise exponential backoff with jitter."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return min(2.0**attempt, 30.0) + random.uniform(0, 0.5)


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int = 4,
    sleep: Any = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying transport errors and retryable status codes.

    The final response is returned even if it is an error, so callers can raise
    a service-specific exception with the body attached.
    """
    last_response: Optional[httpx.Response] = None
    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == max_attempts - 1:
                raise
            delay = _sleep_for(attempt, None)
            log.warning("%s %s failed (%s); retrying in %.1fs", method, url, exc, delay)
            sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUSES or attempt == max_attempts - 1:
            return response

        last_response = response
        delay = _sleep_for(attempt, response)
        log.warning(
            "%s %s returned %s; retrying in %.1fs", method, url, response.status_code, delay
        )
        sleep(delay)

    # Unreachable in practice: the loop either returns or raises.
    assert last_response is not None
    return last_response
