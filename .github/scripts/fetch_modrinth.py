#!/usr/bin/env python3
"""
Talk to the Modrinth HTTP API.

Shared by build_artifact, publish_to_modrinth, and import_modrinth_history.

Provides:
  - Simple GET helpers that either return parsed JSON or abort with SystemExit.
  - Transient-error classification and exponential-backoff timing used by the
    publish retry loop.
  - A minimal multipart encoder for the version-create endpoint (fields + one
    zip file).

Constants control the retry policy used by callers that implement their own
loops (publish_to_modrinth, import_modrinth_history).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

# Maximum attempts (including the first) for a single HTTP operation that
# may be retried. Callers sleep between attempts according to
# retry_after_seconds.
MAX_ATTEMPTS = 5

# Hard timeout passed to urlopen for long-running requests (zip uploads).
REQUEST_TIMEOUT_SEC = 240

# Upper bound on any computed or server-supplied Retry-After delay.
BACKOFF_CAP_SEC = 60


def http_json(url: str, user_agent: str, timeout: int = 60):
    """
    GET JSON from url.

    Returns the parsed value, or None when the body is empty.
    Raises urllib.error.HTTPError / URLError / json.JSONDecodeError on failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def http_json_or_exit(url: str, user_agent: str, timeout: int = 60):
    """
    Like http_json, but aborts the process with SystemExit on network/HTTP/JSON
    failures so CI logs show a clear reason.
    """
    try:
        return http_json(url, user_agent, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} fetching {url}\n{body}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"Failed to fetch {url}: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON from {url}: {e}") from e


def is_transient(exc: BaseException) -> bool:
    """
    Return True when the exception is considered safe to retry.

    Treated as transient:
      - HTTP 408, 429, and any 5xx
      - urllib.error.URLError (DNS, connection reset, etc.)
      - TimeoutError / OSError

    All other exceptions (including 4xx other than 408/429) are permanent.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (408, 429):
            return True
        if 500 <= exc.code <= 599:
            return True
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, OSError)):
        return True
    return False


def retry_after_seconds(exc: BaseException, attempt: int) -> float:
    """
    Compute the sleep duration before the next retry attempt.

    Prefer a numeric Retry-After header when present (capped at
    BACKOFF_CAP_SEC). Otherwise use exponential backoff 2**attempt,
    also capped. The returned value is always a float seconds.
    """
    if isinstance(exc, urllib.error.HTTPError):
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            try:
                return min(float(ra), float(BACKOFF_CAP_SEC))
            except ValueError:
                pass
    base = min(2**attempt, BACKOFF_CAP_SEC)
    return float(min(base, BACKOFF_CAP_SEC))


def encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """
    Build a multipart/form-data body for the Modrinth version-create endpoint.

    fields  – name → string (or bytes) form fields; the JSON payload is
              conventionally placed under the key "data".
    files   – name → Path; each file is attached with Content-Type
              application/zip and its own filename.

    Returns (body_bytes, content_type_header_value). The boundary string is
    fixed so that the implementation stays simple and deterministic.
    """
    boundary = "----ModrinthBoundary7MA4YWxkTrZu0gW"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(value if isinstance(value, bytes) else str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for name, path in files.items():
        data = path.read_bytes()
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: application/zip\r\n\r\n"
            ).encode()
        )
        body.extend(data)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    content_type = f"multipart/form-data; boundary={boundary}"
    return bytes(body), content_type
