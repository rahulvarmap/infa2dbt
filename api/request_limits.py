from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.responses import JSONResponse

MAX_FILE_SIZE = 5 * 1024 * 1024
MAX_REQUEST_BODY_SIZE = MAX_FILE_SIZE + 128 * 1024
MAX_MAPPINGS = 50
MAX_MULTIPART_FIELDS = 4
MAX_MULTIPART_FIELD_SIZE = 16 * 1024
PROCESSING_TIMEOUT_SECONDS = 10.0
PROCESSING_LOCK_PATH = Path("/tmp/infa2dbt-processing.lock")

PROCESSING_PATHS = {
    "/upload",
    "/api/upload",
    "/parse",
    "/api/parse",
    "/lineage/trace",
    "/api/lineage/trace",
}


def is_processing_request(scope: dict[str, Any]) -> bool:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    path = scope.get("path", "")
    return path in PROCESSING_PATHS or (
        (path.startswith("/samples/") or path.startswith("/api/samples/"))
        and path.endswith("/parse")
    )


def _json_response(
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status_code: int,
    error: str,
) -> Awaitable[None]:
    return JSONResponse({"error": error}, status_code=status_code)(scope, receive, send)


class ProcessingLimitsMiddleware:
    """Bound request bodies and allow one active processing request per host."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if not is_processing_request(scope):
            await self.app(scope, receive, send)
            return

        lock_descriptor: int | None = None
        try:
            lock_descriptor = os.open(
                PROCESSING_LOCK_PATH,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                await _json_response(
                    scope,
                    receive,
                    send,
                    429,
                    "Another upload or XML operation is already in progress. Try again shortly.",
                )
                return
        except OSError:
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            await _json_response(
                scope,
                receive,
                send,
                503,
                "XML processing is temporarily unavailable.",
            )
            return

        try:
            content_lengths = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ]
            if content_lengths:
                if len(content_lengths) != 1 or not content_lengths[0].isdigit():
                    await _json_response(
                        scope, receive, send, 400, "Invalid Content-Length header."
                    )
                    return
                if int(content_lengths[0]) > MAX_REQUEST_BODY_SIZE:
                    await _json_response(
                        scope,
                        receive,
                        send,
                        413,
                        "Request body must be 5 MB or smaller, plus multipart form overhead.",
                    )
                    return

            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_REQUEST_BODY_SIZE:
                    await _json_response(
                        scope,
                        receive,
                        send,
                        413,
                        "Request body must be 5 MB or smaller, plus multipart form overhead.",
                    )
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break

            body_bytes = bytes(body)
            delivered = False

            async def replay_body() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": body_bytes,
                        "more_body": False,
                    }
                return await receive()

            response_started = False

            async def send_with_tracking(message: dict[str, Any]) -> None:
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            await asyncio.wait_for(
                self.app(scope, replay_body, send_with_tracking),
                timeout=PROCESSING_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if not response_started:
                await _json_response(
                    scope,
                    receive,
                    send,
                    504,
                    "XML processing exceeded the 10-second time limit.",
                )
        finally:
            if lock_descriptor is not None:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
