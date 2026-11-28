"""``POST /client-errors``: error reports from the browser code.

The frontend and the Book Portal extension can't write files, so they send
each error here as one event of the shared format without ``ts`` and
``host``; the backend adds both and appends the event to
``client-errors.jsonl`` (``ibp/error_log.py``).

- The body must be ``application/json`` (else a 415), at most 16 KiB
  (larger is a 413), strict JSON (no NaN or Infinity, no number out of
  range) and a valid event (else a 422 naming only the fields and error
  types, never what was sent). ``app`` is ``frontend`` or
  ``book-portal-extension``; ``level`` is WARNING, ERROR or CRITICAL.
- What is kept is bounded: ``msg`` and ``exc`` are cut to
  ``MAX_MSG_CHARS`` and ``MAX_EXC_CHARS``; ``data`` keeps at most
  ``MAX_DATA_KEYS`` keys with scalar values (or short lists of them),
  strings cut to ``MAX_DATA_TEXT_CHARS``, and ``data.url`` and
  ``data.source`` lose any query string or fragment.
- ``host`` is the client address nginx passes in ``X-Real-IP`` when the
  connection comes from a trusted proxy (``[server] trusted_proxies``:
  loopback and Docker's private ranges by default), else the connection's
  own address.
- At most 30 reports a minute are taken from one address, and 300 from all
  of them. Over that they are dropped with a 429, and one WARNING per
  address (or for all of them) and minute says how many were.
- A report that can't be written once the file is open (a full disk, a
  failed rotation) is a 503.
"""

import asyncio
import ipaddress
import json
import logging
import math
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import from_json
from starlette.concurrency import run_in_threadpool

from .base import app, config
from .error_log import WriteFailed, client_error_log, timestamp

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
RATE_LIMIT = 30
GLOBAL_RATE_LIMIT = 300
RATE_WINDOW_S = 60.0

MAX_MSG_CHARS = 1024
MAX_EXC_CHARS = 8192
MAX_DATA_KEYS = 20
MAX_DATA_KEY_CHARS = 64
MAX_DATA_TEXT_CHARS = 500
MAX_DATA_LIST_ITEMS = 20
# Integers beyond this are refused like non-finite floats: no JSON reader
# keeps them exact.
MAX_SAFE_INTEGER = 2**53 - 1
# data keys that hold a URL (or ``url:line:col``), kept without the query
# string or fragment, which can hold inmate numbers.
URL_KEYS = frozenset({"url", "source"})

DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8, ::1, 172.16.0.0/12"

Level = Literal["WARNING", "ERROR", "CRITICAL"]
Scalar = str | int | float | bool | None


def truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the last one an ellipsis."""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def strip_query(value: str) -> str:
    """A URL (or ``url:line:col``) without its query string or fragment."""
    match = re.fullmatch(r"(.*?)((?::\d+){1,2})?", value, flags=re.DOTALL)
    url, position = (match.group(1), match.group(2) or "") if match else (value, "")
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(query="", fragment="")) + position


def clean_scalar(value: Any) -> tuple[bool, Scalar]:
    """``(True, value)`` for a scalar (a string cut short), else ``(False,
    None)``."""
    if isinstance(value, str):
        return True, truncate(value, MAX_DATA_TEXT_CHARS)
    if value is None or isinstance(value, (bool, int, float)):
        return True, value
    return False, None


def clean_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """``data`` bounded: at most ``MAX_DATA_KEYS`` keys of at most
    ``MAX_DATA_KEY_CHARS`` characters, each value a scalar or a list of at
    most ``MAX_DATA_LIST_ITEMS`` scalars (anything else is left out), and
    the URL keys without their query string or fragment."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if len(cleaned) >= MAX_DATA_KEYS:
            break
        if len(key) > MAX_DATA_KEY_CHARS:
            continue
        if isinstance(value, list):
            items = [clean_scalar(item) for item in value[:MAX_DATA_LIST_ITEMS]]
            if not all(ok for ok, _ in items):
                continue
            cleaned[key] = [item for _, item in items]
            continue
        ok, scalar = clean_scalar(value)
        if not ok:
            continue
        if key in URL_KEYS and isinstance(scalar, str):
            scalar = strip_query(scalar)
        cleaned[key] = scalar
    return cleaned or None


class ClientErrorReport(BaseModel):
    """One browser event; any other field (``ts``, ``host``) is ignored."""

    model_config = ConfigDict(extra="ignore")

    level: Level
    app: Literal["frontend", "book-portal-extension"]
    version: str | None = Field(default=None, max_length=100)
    logger: str | None = Field(default=None, max_length=200)
    msg: str
    exc: str | None = None
    data: dict[str, Any] | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("msg")
    @classmethod
    def _cut_msg(cls, value: str) -> str:
        return truncate(value, MAX_MSG_CHARS)

    @field_validator("exc")
    @classmethod
    def _cut_exc(cls, value: str | None) -> str | None:
        return None if value is None else truncate(value, MAX_EXC_CHARS)

    @field_validator("data")
    @classmethod
    def _bound_data(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else clean_data(value)


FIELDS = frozenset(ClientErrorReport.model_fields)


class InvalidReport(Exception):
    """A report that is refused with a 422; ``errors`` are safe to return."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("invalid report")
        self.errors = errors


def safe_errors(error: ValidationError) -> list[dict[str, str]]:
    """The field (a top-level one, never a key the client chose) and error
    type of each validation error, without the input or its message."""
    found = []
    for item in error.errors(include_url=False, include_input=False):
        known = [part for part in item["loc"] if part in FIELDS]
        entry = {"field": str(known[0]) if known else "", "type": item["type"]}
        if entry not in found:
            found.append(entry)
    return found


def out_of_range(value: Any) -> bool:
    """Whether ``value`` holds, anywhere, a non-finite float or an integer
    beyond ``MAX_SAFE_INTEGER``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, int):
        return abs(value) > MAX_SAFE_INTEGER
    if isinstance(value, dict):
        return any(out_of_range(item) for item in value.values())
    if isinstance(value, list):
        return any(out_of_range(item) for item in value)
    return False


def parse_report(body: bytes) -> ClientErrorReport:
    """The report in ``body``; raises :class:`InvalidReport`.

    Strict JSON: malformed UTF-8, a lone surrogate, NaN or Infinity, nesting
    deeper than the parser's limit, and (anywhere, ignored fields too) a
    number that is out of range are all refused.
    """
    try:
        parsed = from_json(body, allow_inf_nan=False)
    except ValueError as error:
        raise InvalidReport([{"field": "", "type": "json_invalid"}]) from error
    if not isinstance(parsed, dict):
        raise InvalidReport([{"field": "", "type": "model_type"}])
    bad = [key for key, value in parsed.items() if out_of_range(value)]
    if bad:
        raise InvalidReport(
            [
                {"field": key if key in FIELDS else "", "type": "number_out_of_range"}
                for key in dict.fromkeys(bad)
            ]
        )
    try:
        return ClientErrorReport.model_validate(parsed)
    except ValidationError as error:
        raise InvalidReport(safe_errors(error)) from error


@dataclass
class _Window:
    start: float
    count: int = 0
    dropped: int = 0


@dataclass
class RateLimiter:
    """At most ``limit`` reports per address, and ``global_limit`` from all
    addresses, in each ``window_s`` window.

    Each window starts at its first report. When a window that dropped
    reports ends, one WARNING says how many: on the next report from any
    address, or from a timer set at the first drop, whichever comes first.
    A report one address may send but the global window has no room for is
    counted as the global window's drop.
    """

    limit: int = RATE_LIMIT
    window_s: float = RATE_WINDOW_S
    clock: Callable[[], float] = time.monotonic
    global_limit: int = GLOBAL_RATE_LIMIT
    _windows: dict[str, _Window] = field(default_factory=dict, init=False)
    _global: _Window | None = field(default=None, init=False)

    def allow(self, address: str) -> bool:
        """Count a report from ``address``; False if it is over a limit."""
        now = self.clock()
        self.flush(now)
        window = self._windows.setdefault(address, _Window(start=now))
        if self._global is None:
            self._global = _Window(start=now)
        if window.count >= self.limit:
            self._drop(window, now)
            return False
        if self._global.count >= self.global_limit:
            self._drop(self._global, now)
            return False
        window.count += 1
        self._global.count += 1
        return True

    def _drop(self, window: _Window, now: float) -> None:
        window.dropped += 1
        if window.dropped == 1:
            self._schedule_flush(window.start + self.window_s - now)

    def flush(self, now: float | None = None) -> None:
        """Forget ended windows, reporting the drops of each."""
        now = self.clock() if now is None else now
        ended = [
            (address, window)
            for address, window in self._windows.items()
            if now - window.start >= self.window_s
        ]
        for address, window in ended:
            del self._windows[address]
            if window.dropped:
                logger.warning(
                    "dropped %d client error reports from %s over the limit "
                    "of %d a minute",
                    window.dropped,
                    address,
                    self.limit,
                    extra={"data": {"address": address, "dropped": window.dropped}},
                )
        if self._global is not None and now - self._global.start >= self.window_s:
            dropped, self._global = self._global.dropped, None
            if dropped:
                logger.warning(
                    "dropped %d client error reports from all addresses over "
                    "the limit of %d a minute",
                    dropped,
                    self.global_limit,
                    extra={"data": {"address": "all", "dropped": dropped}},
                )

    def _schedule_flush(self, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a test calling allow() directly)
            return
        loop.call_later(max(delay, 0.0) + 0.1, self.flush)


rate_limiter = RateLimiter()

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def get_trusted_proxies() -> list[IPNetwork]:
    """The networks whose connections may set the client address with
    ``X-Real-IP``: ``[server] trusted_proxies`` (comma or whitespace
    separated addresses or networks). An entry that isn't one is skipped
    with a WARNING."""
    value = config.get("server", "trusted_proxies", fallback=DEFAULT_TRUSTED_PROXIES)
    networks = []
    for entry in re.split(r"[,\s]+", value):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("ignoring trusted_proxies entry %r: not a network", entry)
    return networks


trusted_proxies = get_trusted_proxies()


def parse_address(text: str) -> IPAddress | None:
    """``text`` as an address (an IPv4-mapped IPv6 one as IPv4), or None."""
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def client_address(request: Request) -> str:
    """The client's address: nginx's ``X-Real-IP`` if the connection is
    from a trusted proxy, else the connection's own."""
    peer = request.client.host if request.client else ""
    peer_address = parse_address(peer)
    if peer_address is None:
        return peer or "unknown"
    if any(peer_address in network for network in trusted_proxies):
        forwarded = parse_address(request.headers.get("x-real-ip", ""))
        if forwarded is not None:
            return str(forwarded)
    return str(peer_address)


def is_json(request: Request) -> bool:
    """Whether the body is declared ``application/json`` (any parameters)."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    return media_type.strip().lower() == "application/json"


async def read_body(request: Request) -> bytes | None:
    """The body, or None if it is over ``MAX_BODY_BYTES``."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_BODY_BYTES:
                return None
        except ValueError:
            pass
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def json_response(status_code: int, content: dict[str, Any]) -> Response:
    """A JSON answer with a body the server chose."""
    return Response(
        content=json.dumps(content),
        media_type="application/json",
        status_code=status_code,
    )


@app.post(
    "/client-errors",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ClientErrorReport.model_json_schema()}
            },
        }
    },
)
async def report_client_error(request: Request) -> Response:
    """Record one error from the browser code (frontend, extension)."""
    received = time.time()
    if not is_json(request):
        return json_response(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"detail": "Report must be application/json."},
        )
    address = client_address(request)
    if not rate_limiter.allow(address):
        return Response(status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    body = await read_body(request)
    if body is None:
        return json_response(
            status.HTTP_413_CONTENT_TOO_LARGE, {"detail": "Report larger than 16 KiB."}
        )
    try:
        report = parse_report(body)
    except InvalidReport as error:
        return json_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"detail": "Invalid report.", "errors": error.errors},
        )

    event: dict[str, Any] = {
        "ts": timestamp(received),
        "level": report.level,
        "app": report.app,
        "version": report.version,
        "logger": report.logger,
        "host": address,
        "msg": report.msg,
        "exc": report.exc,
        "data": report.data,
    }
    try:
        await run_in_threadpool(
            client_error_log.write, {k: v for k, v in event.items() if v is not None}
        )
    except WriteFailed:
        return json_response(
            status.HTTP_503_SERVICE_UNAVAILABLE, {"detail": "Report not recorded."}
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
