"""POST /client-errors: browser error reports appended to client-errors.jsonl."""

# pylint: disable=redefined-outer-name

import errno
import json
import logging
import logging.handlers
import os
import re
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ibp import client_errors
from ibp.base import app
from ibp.error_log import CLIENT_LOG_NAME, client_error_log

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")
# nginx in the frontend container, on the stack's Docker network.
PROXY = ("172.18.0.3", 40000)
JSON = {"Content-Type": "application/json"}
INVALID = {"detail": "Invalid report."}


@pytest.fixture(autouse=True)
def enabled_loggers():
    """Undo an earlier test's ``logging.config.fileConfig`` (alembic's
    env.py), which disables the loggers that existed when it ran."""
    for name in ("ibp.base", "ibp.error_log", "ibp.client_errors"):
        logging.getLogger(name).disabled = False


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh log directory, and the file closed before and after."""
    directory = tmp_path / "logs"
    monkeypatch.setenv("IBP_LOG_DIR", str(directory))
    client_error_log.close()
    yield directory
    client_error_log.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """A fresh rate limiter on a fake clock."""
    fake = FakeClock()
    monkeypatch.setattr(
        client_errors, "rate_limiter", client_errors.RateLimiter(clock=fake)
    )
    return fake


@pytest.fixture
def client(log_dir, clock) -> TestClient:  # pylint: disable=unused-argument
    """Client without the lifespan, connecting from nginx's address; the
    file opens on the first report."""
    return TestClient(app, client=PROXY)


def direct(address: str) -> TestClient:
    """A client connecting from ``address`` itself, not through nginx."""
    return TestClient(app, client=(address, 50000))


def report(**fields) -> dict:
    """A valid report with ``fields`` changed."""
    body = {
        "level": "ERROR",
        "app": "frontend",
        "version": "v1.0.22",
        "logger": "window.onerror",
        "msg": "TypeError: x is undefined",
        "exc": "TypeError: x is undefined\n    at f (index.js:1:2)",
        "data": {"url": "/packages"},
    }
    body.update(fields)
    return {key: value for key, value in body.items() if value is not None}


def events(log_dir: Path) -> list[dict]:
    """The events written so far."""
    path = log_dir / CLIENT_LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def drops(caplog) -> list[logging.LogRecord]:
    """The rate limiter's WARNINGs captured so far."""
    return [r for r in caplog.records if r.name == "ibp.client_errors"]


def test_report_is_appended_with_ts_and_host(client, log_dir):
    """The server adds ts (with its UTC offset) and host (X-Real-IP)."""
    response = client.post(
        "/client-errors", json=report(), headers={"X-Real-IP": "100.65.137.125"}
    )
    assert response.status_code == 204
    assert response.content == b""
    (event,) = events(log_dir)
    assert TS.match(event.pop("ts"))
    assert event == {
        "level": "ERROR",
        "app": "frontend",
        "version": "v1.0.22",
        "logger": "window.onerror",
        "host": "100.65.137.125",
        "msg": "TypeError: x is undefined",
        "exc": "TypeError: x is undefined\n    at f (index.js:1:2)",
        "data": {"url": "/packages"},
    }


def test_client_ts_host_and_unknown_fields_are_ignored(client, log_dir):
    """Only the server's ts and host are written; unknown fields are dropped."""
    body = report(ts="1999-01-01T00:00:00Z", host="spoofed", extra="x")
    response = client.post(
        "/client-errors", json=body, headers={"X-Real-IP": "192.168.1.26"}
    )
    assert response.status_code == 204
    (event,) = events(log_dir)
    assert event["host"] == "192.168.1.26"
    assert not event["ts"].startswith("1999")
    assert "extra" not in event


def test_optional_fields_left_out(client, log_dir):
    """A minimal report writes only what it has, level upper-cased."""
    body = {"level": "warning", "app": "book-portal-extension", "msg": "odd"}
    assert client.post("/client-errors", json=body).status_code == 204
    (event,) = events(log_dir)
    assert set(event) == {"ts", "level", "app", "host", "msg"}
    assert event["level"] == "WARNING"
    assert event["app"] == "book-portal-extension"


def test_host_without_x_real_ip_is_the_connection(client, log_dir):
    """Without nginx's header (or with a bad one) the peer address is used."""
    client.post("/client-errors", json=report())
    client.post("/client-errors", json=report(), headers={"X-Real-IP": "not an ip"})
    assert [event["host"] for event in events(log_dir)] == [PROXY[0]] * 2


@pytest.mark.parametrize(
    "peer, expected",
    [
        # A tailnet or LAN client reaching the backend directly can't name
        # another address.
        ("100.65.137.125", "100.65.137.125"),
        ("192.168.1.26", "192.168.1.26"),
        # Not an address at all (Starlette's test client).
        ("testclient", "testclient"),
    ],
)
def test_x_real_ip_from_an_untrusted_peer_is_ignored(
    log_dir, clock, peer, expected
):  # pylint: disable=unused-argument
    """Only a trusted proxy's X-Real-IP is believed."""
    response = direct(peer).post(
        "/client-errors", json=report(), headers={"X-Real-IP": "10.9.9.9"}
    )
    assert response.status_code == 204
    assert [event["host"] for event in events(log_dir)] == [expected]


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:172.20.0.2"])
def test_x_real_ip_from_loopback_and_docker_is_used(
    log_dir, clock, peer
):  # pylint: disable=unused-argument
    """Loopback and Docker's private range (an IPv4-mapped one too) are
    trusted by default."""
    response = direct(peer).post(
        "/client-errors", json=report(), headers={"X-Real-IP": "100.120.251.0"}
    )
    assert response.status_code == 204
    assert [event["host"] for event in events(log_dir)] == ["100.120.251.0"]


def test_trusted_proxies_from_config(monkeypatch, caplog):
    """[server] trusted_proxies lists addresses or networks; a bad entry is
    skipped with a WARNING."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    monkeypatch.setitem(
        client_errors.config["server"],
        "trusted_proxies",
        "10.0.0.0/8, nope 192.168.1.5",
    )
    assert [str(net) for net in client_errors.get_trusted_proxies()] == [
        "10.0.0.0/8",
        "192.168.1.5/32",
    ]
    assert "ignoring trusted_proxies entry 'nope'" in caplog.text


def test_a_proxy_left_out_of_trusted_proxies_is_not_believed(
    log_dir, clock, monkeypatch
):  # pylint: disable=unused-argument
    """With the list emptied, even nginx's header is ignored."""
    monkeypatch.setattr(client_errors, "trusted_proxies", [])
    TestClient(app, client=PROXY).post(
        "/client-errors", json=report(), headers={"X-Real-IP": "100.120.251.0"}
    )
    assert [event["host"] for event in events(log_dir)] == [PROXY[0]]


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded"]
)
def test_body_not_declared_json_is_415(client, log_dir, clock, content_type):
    """A simple cross-origin request (text/plain, a form) is refused, and
    doesn't use up the sender's allowance."""
    headers = {"Content-Type": content_type} if content_type else {}
    for _ in range(client_errors.RATE_LIMIT + 1):
        response = client.post(
            "/client-errors", content=json.dumps(report()), headers=headers
        )
        assert response.status_code == 415
        assert response.json() == {"detail": "Report must be application/json."}
    assert not events(log_dir)
    assert client.post("/client-errors", json=report()).status_code == 204


def test_json_with_a_charset_is_taken(client, log_dir):
    """application/json with parameters, in any case, is JSON."""
    response = client.post(
        "/client-errors",
        content=json.dumps(report()),
        headers={"Content-Type": "Application/JSON; charset=utf-8"},
    )
    assert response.status_code == 204
    assert len(events(log_dir)) == 1


def test_what_the_browser_apps_send_is_taken(client, log_dir):
    """The frontend's and the extension's reports as they build them."""
    frontend = {
        "level": "ERROR",
        "app": "frontend",
        "version": "v1.0.22",
        "logger": "react-query.mutation",
        "msg": "ApiError: Internal Server Error",
        "exc": "ApiError: Internal Server Error\n    at r (index-abc.js:1:2)",
        "data": {"status": 500, "url": "/requests"},
    }
    extension = {
        "level": "WARNING",
        "app": "book-portal-extension",
        "version": "0.7.2",
        "logger": "content",
        "msg": "portal didn't confirm the shipment",
        "data": {"refused": True, "package": None, "url": "/Shipment/Add"},
    }
    for body in (frontend, extension):
        response = client.post("/client-errors", content=json.dumps(body), headers=JSON)
        assert response.status_code == 204
    found = events(log_dir)
    assert found[0]["data"] == frontend["data"]
    assert found[1]["data"] == extension["data"]


@pytest.mark.parametrize(
    "body",
    [
        report(app="backend"),
        report(app="shippy"),
        report(level="LOUD"),
        report(level="DEBUG"),
        report(level="info"),
        report(msg=None),
        report(data=["not", "an", "object"]),
        ["not an object"],
    ],
)
def test_invalid_report_is_422(client, log_dir, body):
    """An app other than the browser ones, a level below WARNING, or a
    malformed event, is refused."""
    response = client.post("/client-errors", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == INVALID["detail"]
    assert not events(log_dir)


def test_422_names_fields_and_types_never_the_input(client, log_dir):
    """The answer says which field and what kind of error, not what was sent
    (nor a key the client chose)."""
    body = report(logger="secret " * 50, level="LOUD", data={"secret key": [{}]})
    response = client.post("/client-errors", json=body)
    assert response.status_code == 422
    assert response.json() == {
        **INVALID,
        "errors": [
            {"field": "level", "type": "literal_error"},
            {"field": "logger", "type": "string_too_long"},
        ],
    }
    assert "secret" not in response.text
    assert not events(log_dir)


@pytest.mark.parametrize(
    "body, errors",
    [
        # Out of range for a double: the parser makes it inf.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": 1e400}',
            [{"field": "msg", "type": "number_out_of_range"}],
        ),
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": -1e999}}',
            [{"field": "data", "type": "number_out_of_range"}],
        ),
        # Anywhere, even in a field that would be ignored.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "ts": [1e400]}',
            [{"field": "", "type": "number_out_of_range"}],
        ),
        # Beyond what a JSON reader keeps exact.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": 9007199254740992}}',
            [{"field": "data", "type": "number_out_of_range"}],
        ),
        # Beyond what the parser takes at all.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": '
            + b"9" * 5000
            + b"}}",
            [{"field": "", "type": "json_invalid"}],
        ),
        # Not JSON: the non-finite literals.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": NaN}}',
            [{"field": "", "type": "json_invalid"}],
        ),
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": Infinity}}',
            [{"field": "", "type": "json_invalid"}],
        ),
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": {"n": -Infinity}}',
            [{"field": "", "type": "json_invalid"}],
        ),
        # Malformed UTF-8, and a lone surrogate (which UTF-8 can't write).
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "\xff\xfe secret"}',
            [{"field": "", "type": "json_invalid"}],
        ),
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "\\ud800 secret"}',
            [{"field": "", "type": "json_invalid"}],
        ),
        # Nested past the parser's limit.
        (
            b'{"level": "ERROR", "app": "frontend", "msg": "x", "data": '
            + b'{"a": ' * 300
            + b"1"
            + b"}" * 301,
            [{"field": "", "type": "json_invalid"}],
        ),
        (
            b'[{"level": "ERROR", "app": "frontend", "msg": "x"}]',
            [{"field": "", "type": "model_type"}],
        ),
    ],
    ids=[
        "overflowing-float",
        "overflowing-float-in-data",
        "overflowing-float-in-ignored-field",
        "unsafe-integer",
        "huge-integer",
        "nan",
        "infinity",
        "minus-infinity",
        "malformed-utf8",
        "lone-surrogate",
        "deep-nesting",
        "not-an-object",
    ],
)
def test_strict_json_is_required(client, log_dir, body, errors):
    """Each is a fixed 422 (never a 500, never the input echoed), and
    nothing is written."""
    response = client.post("/client-errors", content=body, headers=JSON)
    assert response.status_code == 422
    assert response.json() == {**INVALID, "errors": errors}
    assert "secret" not in response.text
    assert not events(log_dir)


def test_invalid_json_is_422(client, log_dir):
    """A body that isn't JSON is refused."""
    response = client.post("/client-errors", content=b"{not json", headers=JSON)
    assert response.status_code == 422
    assert response.json() == {
        **INVALID,
        "errors": [{"field": "", "type": "json_invalid"}],
    }
    assert not events(log_dir)


def test_msg_and_exc_are_cut_short(client, log_dir):
    """msg and exc are kept to their caps, ending in an ellipsis."""
    msg = "m" * (client_errors.MAX_MSG_CHARS + 10)
    exc = "e" * client_errors.MAX_EXC_CHARS
    client.post("/client-errors", json=report(msg=msg, exc=exc + "tail"))
    (event,) = events(log_dir)
    assert event["msg"] == "m" * (client_errors.MAX_MSG_CHARS - 1) + "…"
    assert event["exc"] == "e" * (client_errors.MAX_EXC_CHARS - 1) + "…"
    client.post("/client-errors", json=report(msg="short", exc="e" * 100))
    assert events(log_dir)[1]["msg"] == "short"
    assert events(log_dir)[1]["exc"] == "e" * 100


def test_data_is_bounded(client, log_dir):
    """Scalars (and short lists of them) are kept, strings cut short, at
    most MAX_DATA_KEYS keys; nested objects and overlong keys are left out."""
    limit = client_errors.MAX_DATA_TEXT_CHARS
    data = {
        "status": 500,
        "detail": "d" * (limit + 50),
        "queued": True,
        "package": None,
        "ratio": 0.5,
        "missing": ["isbn", "title"],
        "nested": {"a": 1},
        "mixed": ["ok", {"a": 1}],
        "k" * (client_errors.MAX_DATA_KEY_CHARS + 1): 1,
        "long list": list(range(30)),
    }
    data.update({f"extra{i}": i for i in range(30)})
    client.post("/client-errors", json=report(data=data))
    (event,) = events(log_dir)
    kept = event["data"]
    assert len(kept) == client_errors.MAX_DATA_KEYS
    assert kept["status"] == 500
    assert kept["detail"] == "d" * (limit - 1) + "…"
    assert kept["queued"] is True and kept["package"] is None
    assert kept["ratio"] == 0.5
    assert kept["missing"] == ["isbn", "title"]
    assert kept["long list"] == list(range(client_errors.MAX_DATA_LIST_ITEMS))
    assert "nested" not in kept and "mixed" not in kept
    assert not any(key.startswith("kkk") for key in kept)
    assert list(kept)[-1] == "extra12"  # 7 kept above, then 13 more


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"url": "/inmates/12345?q=SMITH#top"}, {"url": "/inmates/12345"}),
        (
            {"url": "http://ibp-server/requests?inmate=01234567"},
            {"url": "http://ibp-server/requests"},
        ),
        (
            {"source": "http://ibp-server/assets/index-abc.js?v=1#x:12:34"},
            {"source": "http://ibp-server/assets/index-abc.js:12:34"},
        ),
        (
            {"source": "http://ibp-server/a.js:5:6"},
            {"source": "http://ibp-server/a.js:5:6"},
        ),
        ({"nested": {"a": 1}}, None),
    ],
)
def test_data_urls_lose_query_and_fragment(client, log_dir, data, expected):
    """data.url and data.source are kept without the query string or
    fragment; data left empty is left out."""
    client.post("/client-errors", json=report(data=data))
    (event,) = events(log_dir)
    assert event.get("data") == expected


def test_over_16_kib_is_413(client, log_dir):
    """The body may be 16 KiB; one byte more is a 413."""
    base = json.dumps(report(exc=""))
    padding = client_errors.MAX_BODY_BYTES - len(base.encode())
    at_limit = json.dumps(report(exc="x" * padding))
    assert len(at_limit.encode()) == client_errors.MAX_BODY_BYTES
    headers = {"Content-Type": "application/json"}
    response = client.post("/client-errors", content=at_limit, headers=headers)
    assert response.status_code == 204

    over = json.dumps(report(exc="x" * (padding + 1)))
    response = client.post("/client-errors", content=over, headers=headers)
    assert response.status_code == 413
    assert len(events(log_dir)) == 1


def test_over_16_kib_without_content_length_is_413(client, log_dir):
    """A chunked body (no Content-Length) is cut off at the limit too."""

    def chunks():
        yield b'{"level": "ERROR", "app": "frontend", "msg": "'
        for _ in range(20):
            yield b"x" * 1024
        yield b'"}'

    response = client.post(
        "/client-errors",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert not events(log_dir)


def test_rate_limit_per_address(client, log_dir, clock, caplog):
    """30 a minute per address; the rest are dropped, then one WARNING says
    how many once the minute is over."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    one = {"X-Real-IP": "100.120.251.0"}
    other = {"X-Real-IP": "100.65.137.125"}
    statuses = [
        client.post("/client-errors", json=report(), headers=one).status_code
        for _ in range(33)
    ]
    assert statuses == [204] * 30 + [429] * 3
    # Another address has its own allowance.
    assert (
        client.post("/client-errors", json=report(), headers=other).status_code == 204
    )
    assert len(events(log_dir)) == 31
    assert not drops(caplog)

    clock.now += client_errors.RATE_WINDOW_S
    assert client.post("/client-errors", json=report(), headers=one).status_code == 204
    (record,) = drops(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "dropped 3 client error reports from 100.120.251.0 over the limit of "
        "30 a minute"
    )
    assert len(events(log_dir)) == 32


def test_global_rate_limit(caplog):
    """At most global_limit reports a minute from all addresses together;
    the drops are one WARNING of their own."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    clock = FakeClock()
    limiter = client_errors.RateLimiter(
        limit=2, global_limit=3, window_s=60, clock=clock
    )
    allowed = [limiter.allow(address) for address in "aabbcc"]
    assert allowed == [True, True, True, False, False, False]
    assert not drops(caplog)
    clock.now += 60
    assert limiter.allow("c")
    assert [record.getMessage() for record in drops(caplog)] == [
        "dropped 3 client error reports from all addresses over the limit of 3 "
        "a minute"
    ]


def test_global_rate_limit_at_the_endpoint(client, log_dir):
    """300 a minute from all addresses by default; more is a 429."""
    assert client_errors.GLOBAL_RATE_LIMIT == 300
    limiter = client_errors.rate_limiter
    limiter.global_limit = 4
    statuses = [
        client.post(
            "/client-errors", json=report(), headers={"X-Real-IP": f"10.0.0.{i}"}
        ).status_code
        for i in range(6)
    ]
    assert statuses == [204] * 4 + [429] * 2
    assert len(events(log_dir)) == 4


def test_rate_limiter_reports_each_window_once(caplog):
    """A window's drops are reported once, and a window without drops not at
    all; ended windows are forgotten."""
    caplog.set_level(logging.WARNING, logger="ibp.client_errors")
    clock = FakeClock()
    limiter = client_errors.RateLimiter(limit=2, window_s=60, clock=clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, False, False]
    assert limiter.allow("b")
    clock.now += 60
    limiter.flush()
    limiter.flush()
    assert [record.getMessage() for record in drops(caplog)] == [
        "dropped 2 client error reports from a over the limit of 2 a minute"
    ]
    assert not limiter._windows  # pylint: disable=protected-access


def test_unwritable_log_dir_falls_back_to_stderr(
    tmp_path, monkeypatch, clock, capsys
):  # pylint: disable=unused-argument
    """If the file can't be opened the report still succeeds, on stderr."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("IBP_LOG_DIR", str(blocker / "logs"))
    client_error_log.close()
    try:
        response = TestClient(app).post("/client-errors", json=report(msg="kept"))
    finally:
        client_error_log.close()
    assert response.status_code == 204
    err = capsys.readouterr().err
    assert '"msg": "kept"' in err


def no_space(*_args, **_kwargs):
    """A write to a full disk."""
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def test_full_disk_after_open_is_503(client, log_dir, monkeypatch, caplog):
    """Once the file is open, a write that fails is a 503, said once (with
    no report text) until a write succeeds again."""
    caplog.set_level(logging.WARNING, logger="ibp.error_log")
    assert client.post("/client-errors", json=report(msg="first")).status_code == 204
    handler = client_error_log.open()
    assert isinstance(handler, logging.FileHandler)
    monkeypatch.setattr(handler.stream, "write", no_space)
    for _ in range(2):
        response = client.post("/client-errors", json=report(msg="secret"))
        assert response.status_code == 503
        assert response.json() == {"detail": "Report not recorded."}
    failures = [r for r in caplog.records if r.name == "ibp.error_log"]
    assert [r.getMessage() for r in failures] == [
        "Not writing client-errors.jsonl: [Errno 28] No space left on device; "
        "reports are refused (503) until it can be written"
    ]
    assert "secret" not in caplog.text

    monkeypatch.undo()
    assert client.post("/client-errors", json=report(msg="again")).status_code == 204
    assert [event["msg"] for event in events(log_dir)] == ["first", "again"]


def test_failed_rotation_is_503(client, monkeypatch):
    """A rotation that fails (the rename refused) is a 503, not a 204."""
    assert client.post("/client-errors", json=report()).status_code == 204
    handler = client_error_log.open()
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    monkeypatch.setattr(handler, "maxBytes", 10)

    def refuse(_source, _dest):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(handler, "rotator", refuse)
    assert client.post("/client-errors", json=report()).status_code == 503
    monkeypatch.undo()
    assert client.post("/client-errors", json=report()).status_code == 204


def test_client_log_file_is_0640(client, log_dir):
    """The browser reports' file is readable by its owner and group only."""
    old = os.umask(0)
    try:
        assert client.post("/client-errors", json=report()).status_code == 204
    finally:
        os.umask(old)
    path = log_dir / CLIENT_LOG_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o750


def test_secrets_are_scrubbed_before_writing(client, log_dir):
    """A password in data.detail (quoted, spaces and all), a Bearer token in
    exc, a key= in a URL and a token in a list are replaced; inmate details
    are kept."""
    body = report(
        msg="GET https://books.example/v1?q=isbn:1&key=AIzaSyFAKE0123456789abcd failed",
        exc="Error: refused\n  Authorization: Bearer abc.DEF-123456789xyz\n"
        "    at r (http://ibp-server/assets/index-DkS8f3jK.js:1:2345)",
        data={
            "detail": 'login failed: password: "correct horse battery" for SMITH, '
            "JOHN 01234567 at 2001 Main St",
            "url": "https://user:hunter2@ibp-server/requests?token=x",
            "notes": ["fine", "api_key=sk-FAKE-0123456789abcdefghijkl"],
            "status": 500,
        },
    )
    assert client.post("/client-errors", json=body).status_code == 204
    (event,) = events(log_dir)
    assert event["msg"] == ("GET https://books.example/v1?q=isbn:1&key=<secret> failed")
    assert event["exc"] == (
        "Error: refused\n  Authorization: Bearer <secret>\n"
        "    at r (http://ibp-server/assets/index-DkS8f3jK.js:1:2345)"
    )
    assert event["data"] == {
        "detail": "login failed: password: <secret> for SMITH, JOHN 01234567 "
        "at 2001 Main St",
        "url": "https://<secret>@ibp-server/requests",
        "notes": ["fine", "api_key=<secret>"],
        "status": 500,
    }
    text = (log_dir / CLIENT_LOG_NAME).read_text()
    for secret in ("AIzaSy", "abc.DEF", "horse", "hunter2", "sk-FAKE"):
        assert secret not in text
