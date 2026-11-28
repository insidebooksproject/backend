"""Tests for scripts/error-digest/ibp-error-digest, on made-up log trees.

No test reaches the network or this machine's journal: the journal and
systemctl are a stand-in command, the deploy versions a stand-in fetch.
Every name, number and address below is invented.
"""

# pylint: disable=redefined-outer-name

import grp
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "error-digest"
    / "ibp-error-digest"
)
_loader = importlib.machinery.SourceFileLoader("ibp_error_digest", str(SCRIPT))
_spec = importlib.util.spec_from_loader("ibp_error_digest", _loader)
assert _spec is not None
digest = importlib.util.module_from_spec(_spec)
sys.modules["ibp_error_digest"] = digest  # dataclasses look the module up
_loader.exec_module(digest)

# Thursday 2026-11-26 09:00 in Austin: ISO week 48.
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
CT = timezone(timedelta(hours=-6))

# Invented data that must never reach a digest: an e-mail address in a
# message, and what is only in a ``data`` field (never copied). Inmate names,
# numbers and codes in a message are kept (jstarr 2026-11-28).
SECRETS = [
    "4521 Fake Street",
    "jane@example.org",
]

PY_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\shippy\\gui\\workers\\shipment_worker.py", line {line}, in run\n'
    "    self.buy()\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\shippy\\core\\easypost.py", line 88, in buy\n'
    "    return client.post(url)\n"
    '  File "C:\\Users\\insid\\AppData\\Local\\uv\\tools\\shippy\\Lib\\site-packages'
    '\\httpx\\_client.py", line 1100, in post\n'
    "    raise ConnectError(msg)\n"
    "httpx.ConnectError: {message}"
)


def event(ts, level="ERROR", msg="label purchase failed", **extra):
    return {
        "ts": ts.astimezone(CT).isoformat(timespec="milliseconds"),
        "level": level,
        "logger": "shippy.gui.workers.shipment_worker",
        "host": "IBP-PIPPIN",
        "pid": 4242,
        "thread": "MainThread",
        "attempt_id": "-",
        "msg": msg,
        **extra,
    }


def write_jsonl(path: Path, lines, mtime=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line if isinstance(line, str) else json.dumps(line))
            handle.write("\n")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


class FakeSystem:
    """journalctl and systemctl on a made-up Pi."""

    def __init__(self, entries=(), failed=(), journal_status=0):
        self.entries = list(entries)
        self.failed = list(failed)
        self.journal_status = journal_status
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "journalctl":
            out = "\n".join(json.dumps(e) for e in self.entries)
            return subprocess.CompletedProcess(argv, self.journal_status, out, "")
        out = "".join(f"{u} loaded failed failed Something\n" for u in self.failed)
        return subprocess.CompletedProcess(argv, 0, out, "")


def fake_fetch(url):
    versions = {"shippy": "0.2.0", "printing": "0.5.0", "kiosk": "0.1.0"}
    for repo, version in versions.items():
        if f"/{repo}/deploy/" in url:
            return f'[project]\nname = "x"\nversion = "{version}"\n'.encode()
    raise OSError("no such file")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "srv-ibp-logs"
    server = tmp_path / "srv-ibp-logs-server"
    state = tmp_path / "state"
    config = tmp_path / "etc"
    for folder in (root, server, state, config):
        folder.mkdir()
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-not-real\nIBP_TAG=v1.0.22\n")
    return {
        "root": root,
        "server": server,
        "state": state,
        "config": config,
        "env": env,
        "digest": root / "digest",
    }


@pytest.fixture(autouse=True)
def ctime_is_mtime(monkeypatch):
    """A file's ctime here is when the test wrote it; on the Pi it is when
    the station uploaded it. Tests set that time with os.utime (the mtime)."""
    real = digest.file_times

    def file_times(path):
        modified, _ = real(path)
        return modified, modified

    monkeypatch.setattr(digest, "file_times", file_times)
    return real


def options(tree, now=NOW, **overrides):
    return digest.Options(
        logs_root=tree["root"],
        server_logs=tree["server"],
        state_dir=tree["state"],
        config_dir=tree["config"],
        env_file=tree["env"],
        group=overrides.pop("group", ""),
        now=now,
        **overrides,
    )


def run(tree, now=NOW, system=None, fetch=fake_fetch, **overrides):
    return digest.digest(
        options(tree, now=now, **overrides), command=system or FakeSystem(), fetch=fetch
    )


def groups_by_app(report):
    return {g["app"]: g for g in report["groups"]}


# --------------------------------------------------------------------------
# Redaction


# Inmate details are kept (largely public, and useful; jstarr 2026-11-28):
# only e-mail addresses and secrets are replaced.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("package TEX-01234567-2 failed", "package TEX-01234567-2 failed"),
        ("mail jane@example.org bounced", "mail <email> bounced"),
        (
            "printer \"BOB\" offline and 'BETTY' too",
            "printer \"BOB\" offline and 'BETTY' too",
        ),
        (
            "lookup failed for inmate Jane Q Example",
            "lookup failed for inmate Jane Q Example",
        ),
        ("bad address: 4521 Fake Street", "bad address: 4521 Fake Street"),
        (
            "tracking 9400111202555842761234 stuck",
            "tracking 9400111202555842761234 stuck",
        ),
        ("retry 3 of 5 in 120 s", "retry 3 of 5 in 120 s"),
        ("two\nlines\there", "two lines here"),
    ],
)
def test_redact(text, expected):
    assert digest.redact(text) == expected


def test_redact_keeps_messages_short_and_ignores_non_text():
    assert len(digest.redact("x" * 1000)) == digest.MAX_TEXT
    assert digest.redact(None) == ""
    assert digest.redact({"name": "Jane"}) == ""


@pytest.mark.parametrize(
    "a, b",
    [
        ("package TEX-01234567-2 failed", "package TEX-07654321-0 failed"),
        ('printer "BOB" offline', 'printer "BETTY" offline'),
        (
            "lookup failed for inmate Jane Q Example",
            "lookup failed for inmate John Doe",
        ),
        ("bad address: 4521 Fake Street", "bad address: 12 Other Rd"),
        ("TEX-01234567-2 name Jane", "TEX-00000001-1 name Bob Smith"),
    ],
)
def test_fingerprints_ignore_inmate_details(a, b):
    assert digest.normalize(digest.redact(a)) == digest.normalize(digest.redact(b))


# --------------------------------------------------------------------------
# Stacks and fingerprints


def test_python_stack_frames_are_short_and_innermost_ibp_frame_found():
    stack = digest.parse_stack(PY_TRACE.format(line=41, message="host 'x' down"))
    assert [f.text() for f in stack.frames] == [
        "shippy/gui/workers/shipment_worker.py:41 run",
        "shippy/core/easypost.py:88 buy",
        "httpx/_client.py:1100 post",
    ]
    assert stack.type == "httpx.ConnectError"
    assert stack.message == "host 'x' down"
    assert stack.innermost().text() == "shippy/core/easypost.py:88 buy"
    assert "insid" not in " ".join(f.text() for f in stack.frames)


def test_same_bug_has_one_fingerprint_whatever_the_message_and_line():
    one = digest.parse_stack(PY_TRACE.format(line=41, message="for TEX-01234567-2"))
    two = digest.parse_stack(PY_TRACE.format(line=43, message="timed out"))
    assert digest.fingerprint("shippy-gui", one, "a") == digest.fingerprint(
        "shippy-gui", two, "b"
    )
    assert digest.fingerprint("shippy-gui", one, "a") != digest.fingerprint(
        "shippy", one, "a"
    )


def test_chained_python_traceback_uses_the_last_exception():
    exc = (
        "Traceback (most recent call last):\n"
        '  File "/app/ibp/books.py", line 10, in lookup\n'
        "    x()\n"
        "KeyError: 'isbn'\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/app/ibp/routes.py", line 20, in get_book\n'
        "    y()\n"
        "ValueError: bad value 'Jane'\n"
    )
    stack = digest.parse_stack(exc)
    assert stack.type == "ValueError"
    assert [f.text() for f in stack.frames] == ["ibp/routes.py:20 get_book"]
    assert stack.message == "bad value 'Jane'"


def test_checkout_paths_with_ibp_in_them_are_not_mistaken_for_ibp_code():
    file, ibp = digest.python_file(
        "/home/x/Source/ibp/shippy/.venv/lib/python3.13/site-packages/httpx/_client.py"
    )
    assert (file, ibp) == ("httpx/_client.py", False)
    assert digest.python_file("/home/x/Source/ibp/backend/ibp/routes.py") == (
        "ibp/routes.py",
        True,
    )
    assert digest.python_file("/usr/local/bin/ibp-error-digest") == (
        "ibp-error-digest",
        True,
    )


def test_js_stacks_chrome_and_firefox_and_bundle_hashes():
    chrome = (
        "TypeError: Cannot read properties of undefined (reading 'name')\n"
        "    at renderRow (http://192.0.2.10/assets/index-BkX9a1_z.js:12:345)\n"
        "    at http://192.0.2.10/assets/vendor-Ab12Cd34.js:1:2\n"
    )
    stack = digest.parse_stack(chrome)
    assert stack.type == "TypeError"
    # A bundle's function names are minified, so they're left out.
    assert stack.frames[-1].text() == "assets/index.js:12"
    assert stack.innermost().file == "assets/index.js"
    rebuilt = chrome.replace("BkX9a1_z", "Zz99yy00").replace(":12:345", ":14:1")
    rebuilt = rebuilt.replace("renderRow", "Xe")
    assert digest.fingerprint("frontend", stack, "x") == digest.fingerprint(
        "frontend", digest.parse_stack(rebuilt), "x"
    )
    firefox = (
        "fillField@moz-extension://0000-1111/content.js:40:7\n"
        "onMessage@moz-extension://0000-1111/background.js:9:3\n"
    )
    stack = digest.parse_stack(firefox)
    assert stack.type == ""
    assert [f.text() for f in stack.frames] == [
        "background.js:9 onMessage",
        "content.js:40 fillField",
    ]
    assert stack.innermost().text() == "content.js:40 fillField"


def test_browser_groups_split_by_logger_and_status_not_by_shared_frames():
    # Two API calls failing in the same response helper: same frames.
    exc = (
        "ApiError: Request failed\n"
        "    at handle (http://192.0.2.10/assets/index-BkX9a1_z.js:3:10)\n"
    )
    stack = digest.parse_stack(exc)
    msg = digest.redact("ApiError: Request failed")

    def fp(logger, status, message=msg, app="frontend"):
        return digest.fingerprint(app, stack, message, logger, status)

    lookup = fp("react-query.query", 502)
    assert fp("react-query.query", 502) == lookup
    assert fp("react-query.mutation", 502) != lookup
    assert fp("react-query.query", 500) != lookup
    assert (
        fp("react-query.query", 502, digest.redact("SyntaxError: bad JSON")) != lookup
    )
    # Numbers in the message don't split a group.
    one = digest.redact("book lookup failed: IBP server HTTP 502")
    two = digest.redact("book lookup failed: IBP server HTTP 503")
    ext = "book-portal-extension"
    assert fp("background.isbn", None, one, ext) == fp(
        "background.isbn", None, two, ext
    )
    assert fp("background.isbn", None, one, ext) != fp("background.upc", None, one, ext)


def test_browser_status_comes_from_data_and_is_shown(tree):
    write_jsonl(
        tree["server"] / "client-errors.jsonl",
        [
            event(
                NOW - timedelta(hours=h),
                app="frontend",
                logger="react-query.query",
                msg="ApiError: Request failed",
                data={"status": status, "url": "/requests"},
                host="192.0.2.26",
            )
            for h, status in ((1, 502), (2, 502), (3, 500), (4, True), (5, "502"))
        ],
    )
    _, report, markdown = run(tree)
    by_status = {g["http_status"]: g["count"] for g in report["groups"]}
    assert by_status == {502: 2, 500: 1, None: 2}
    assert "- HTTP status: 502" in markdown


def test_messages_without_exception_group_by_their_shape():
    one = digest.redact("printer BOB offline after 12 tries for TEX-01234567-2")
    two = digest.redact("printer BOB offline after 3 tries for TEX-7654321-0")
    other = digest.redact("printer BOB out of paper")
    assert digest.fingerprint("shippy", None, one) == digest.fingerprint(
        "shippy", None, two
    )
    assert digest.fingerprint("shippy", None, one) != digest.fingerprint(
        "shippy", None, other
    )
    assert digest.FINGERPRINT.fullmatch(digest.fingerprint("shippy", None, one))


# --------------------------------------------------------------------------
# A whole run


def station_logs(tree, folder="IBP_LAPTOP"):
    base = tree["root"] / folder
    write_jsonl(
        base / "errors-shippy-gui.jsonl",
        [
            event(
                NOW - timedelta(days=1),
                exc=PY_TRACE.format(line=41, message="TEX-01234567-2"),
                data={"name": "Jane Q Example", "address": "4521 Fake Street"},
                app="shippy-gui",
                version="0.1.0",
            ),
            event(
                NOW - timedelta(days=2),
                exc=PY_TRACE.format(line=43, message="timed out"),
                app="shippy-gui",
                version="0.1.0",
            ),
            event(NOW - timedelta(days=9), msg="too old"),  # before the window
            event(NOW - timedelta(hours=1), level="INFO", msg="not an error"),
            "{not json",
            "[1, 2, 3]",
            "",
            json.dumps({"level": "ERROR", "msg": "no time stamp"}),
        ],
    )
    write_jsonl(
        base / "errors-shippy-gui.jsonl.1",
        [
            event(
                NOW - timedelta(days=3),
                level="WARNING",
                msg="lookup failed for inmate Jane Q Example (01234567), mail jane@example.org",
                app="shippy-gui",
                version="0.1.0",
            )
        ],
    )
    write_jsonl(
        base / "printer-watcher.jsonl",
        [
            watcher_starting(NOW - timedelta(days=20), "0.4.0"),
            watcher_starting(NOW - timedelta(days=2), "0.5.0"),
            event(
                NOW - timedelta(days=1),
                level="INFO",
                msg="printing starting soon",
                app="watcher",
                data={"version": "9.9.9"},
            ),
        ],
    )
    write_jsonl(
        base / "printer-shippy.jsonl",
        [shippy_starting(NOW - timedelta(days=4), "shippy", "0.1.0")],
    )
    return base


def watcher_starting(ts, version):
    """ibp-printing's label watcher: app "watcher", the version in data."""
    return event(
        ts,
        level="INFO",
        msg="label watcher starting",
        app="watcher",
        logger="ibp_printing.apps.watcher",
        data={
            "version": version,
            "argv": ["ibp-label-watcher"],
            "config_file": "C:\\Users\\insid\\watcher.toml",
            "state_dir": "C:\\Users\\insid\\state",
        },
    )


def shippy_starting(ts, app, version):
    """shippy's start_logging: app and version at the top and in data."""
    return event(
        ts,
        level="INFO",
        msg=f"{app} starting",
        app=app,
        version=version,
        logger="shippy.startup",
        data={
            "app": app,
            "version": version,
            "argv": [app, "--request", "TEX-01234567-2"],
            "python": "3.12.7",
            "platform": "win32",
            "error_log": f"errors-{app}.jsonl",
        },
    )


def test_run_writes_a_redacted_digest(tree):
    station_logs(tree)
    status, report, markdown = run(tree)
    assert status == 0
    md = (tree["digest"] / "2026-W48.md").read_text()
    js = (tree["digest"] / "2026-W48.json").read_text()
    seen = (tree["state"] / "seen.json").read_text()
    assert md == markdown + "\n"
    assert json.loads(js) == report
    for text in (md, js, seen):
        for secret in SECRETS:
            assert secret not in text, secret
    assert md.splitlines()[0] == "# IBP error digest 2026-W48: 2 new - run /ibp-errors"


def test_run_groups_counts_and_describes_each_group(tree):
    station_logs(tree)
    _, report, _ = run(tree)
    assert report["summary"]["events"] == 3
    exc_group, msg_group = report["groups"]
    assert exc_group["count"] == 2 and exc_group["status"] == "new"
    assert exc_group["app"] == "shippy-gui"
    assert exc_group["hosts"] == {"IBP_LAPTOP": 2}
    assert exc_group["versions"] == ["0.1.0"]
    assert exc_group["exception"] == {
        "type": "httpx.ConnectError",
        "message": "TEX-01234567-2",
    }
    assert exc_group["frames"][-1] == "httpx/_client.py:1100 post"
    path, line = exc_group["raw_event"].rsplit(":", 1)
    assert path.endswith("IBP_LAPTOP/errors-shippy-gui.jsonl") and line == "1"
    assert msg_group["level"] == "WARNING"
    assert msg_group["message"] == (
        "lookup failed for inmate Jane Q Example (01234567), mail <email>"
    )
    assert msg_group["raw_event"].endswith("errors-shippy-gui.jsonl.1:1")
    # "{not json", "[1, 2, 3]" and the line with no time stamp.
    assert report["unreadable"] == {"IBP_LAPTOP/errors-shippy-gui.jsonl": 3}


def test_stations_and_versions(tree):
    station_logs(tree)
    old = station_logs(tree, "IBP_LAPTOP2")
    long_ago = NOW - timedelta(days=10)
    for path in old.iterdir():
        os.utime(path, (long_ago.timestamp(), long_ago.timestamp()))
    _, report, markdown = run(tree)
    stations = {s["folder"]: s for s in report["stations"]}
    assert stations["IBP_LAPTOP"]["stale"] is False
    assert stations["IBP_LAPTOP"]["hosts"] == ["IBP-PIPPIN"]
    assert stations["IBP_LAPTOP2"]["stale"] is True
    assert "**no upload all week**" in markdown
    versions = {(v["folder"], v["app"]): v for v in report["versions"]}
    assert versions[("IBP_LAPTOP", "watcher")]["version"] == "0.5.0"
    assert versions[("IBP_LAPTOP", "watcher")]["status"] == "current"
    assert versions[("IBP_LAPTOP", "shippy")]["status"] == "old"
    assert versions[("IBP_LAPTOP", "shippy")]["expected"] == "0.2.0"
    # The stale station's events are older than the window's margin: unread.
    assert report["summary"]["events"] == 3


def test_versions_unknown_offline(tree):
    station_logs(tree)
    _, report, _ = run(tree, fetch=None)
    assert {v["status"] for v in report["versions"]} == {"unknown"}


def test_digest_folder_and_symlinks_are_not_read(tree, tmp_path):
    outside = write_jsonl(
        tmp_path / "elsewhere.jsonl", [event(NOW - timedelta(hours=2), msg="planted")]
    )
    base = tree["root"] / "IBP_LAPTOP"
    base.mkdir()
    (base / "errors-shippy.jsonl").symlink_to(outside)
    (tree["root"] / "linked").symlink_to(base)
    write_jsonl(tree["digest"] / "errors-x.jsonl", [event(NOW, msg="in digest")])
    _, report, _ = run(tree)
    assert report["groups"] == []
    assert [s["folder"] for s in report["stations"]] == ["IBP_LAPTOP"]


def test_missing_folders_still_give_a_digest(tree):
    tree["root"].rmdir()
    tree["server"].rmdir()
    status, report, _ = run(tree)
    assert status == 0
    assert any("not there" in p for p in report["problems"])
    assert (tree["digest"] / "2026-W48.md").exists()


def usec(moment):
    return str(int(moment.timestamp() * 1_000_000))


def failed_unit_line(moment, unit, kind="UNIT"):
    """A failed-units-<day>.jsonl line as ibp-kiosk-log-upload writes it."""
    return {
        "__REALTIME_TIMESTAMP": usec(moment),
        "_HOSTNAME": "ibp-bilbo",
        "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
        "PRIORITY": "3",
        "MESSAGE": f"{unit} failed",
        kind: unit,
    }


def kiosk_host(tree, station="IBP_KIOSK1", host="ibp-bilbo"):
    """A kiosk's upload: <share>/<host>/ inside the station's folder."""
    return tree["root"] / station / host


def test_kiosk_station_layout(tree):
    host = kiosk_host(tree)
    write_jsonl(
        host / "errors-shippy.jsonl",
        [
            event(
                NOW - timedelta(days=1),
                msg="printer BOB offline",
                app="shippy",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "printer-watcher.jsonl",
        [{**watcher_starting(NOW - timedelta(days=3), "0.4.0"), "host": "ibp-bilbo"}],
    )
    write_jsonl(
        host / "print-server" / "errors-print-server.jsonl",
        [
            event(
                NOW - timedelta(hours=6),
                msg="DYMO not found",
                app="print-server",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "print-server" / "printer-print-server.jsonl",
        [
            # The print server's starting line has no version: no row.
            event(
                NOW - timedelta(days=2),
                level="INFO",
                msg="print server starting",
                host="ibp-bilbo",
            )
        ],
    )
    write_jsonl(
        host / "journal" / "journal-2026-11-25.jsonl",
        [
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=1)),
                "PRIORITY": "3",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "_HOSTNAME": "ibp-bilbo",
                "MESSAGE": list(b"tile portal failed for inmate Jane"),
            },
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=1)),
                "PRIORITY": "6",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "MESSAGE": "just info",
            },
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=2)),
                "PRIORITY": "4",
                "_SYSTEMD_UNIT": "ibp-kiosk-update.service",
                "MESSAGE": "update check slow",
            },
            {
                # The uploader's own warning: not a failed unit.
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(days=2)),
                "PRIORITY": "4",
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "MESSAGE": "journal export failed; going on with the upload",
            },
            "garbage",
        ],
    )
    _, report, markdown = run(tree)
    apps = groups_by_app(report)
    assert apps["shippy"]["hosts"] == {"ibp-bilbo": 1}
    assert apps["shippy"]["raw_event"].endswith(
        "IBP_KIOSK1/ibp-bilbo/errors-shippy.jsonl:1"
    )
    assert apps["print-server"]["hosts"] == {"ibp-bilbo": 1}
    assert apps["ibp-kiosk"]["level"] == "ERROR"
    assert apps["ibp-kiosk"]["message"] == "tile portal failed for inmate Jane"
    assert apps["ibp-kiosk-update"]["level"] == "WARNING"
    assert apps["ibp-kiosk-log-upload"]["level"] == "WARNING"
    assert "systemd" not in apps
    assert report["unreadable"] == {
        "IBP_KIOSK1/ibp-bilbo/journal/journal-2026-11-25.jsonl": 1
    }
    assert report["stations"] == [
        {
            "folder": "IBP_KIOSK1",
            "hosts": ["ibp-bilbo"],
            "last_upload": digest.iso(NOW),
            "stale": False,
        }
    ]
    assert [
        (v["folder"], v["app"], v["version"], v["status"]) for v in report["versions"]
    ] == [("IBP_KIOSK1/ibp-bilbo", "watcher", "0.4.0", "old")]
    assert "IBP_KIOSK1 | ibp-bilbo" in markdown


def test_kiosk_failed_units_count_once_a_day_while_failed(tree):
    journal = kiosk_host(tree) / "journal"
    days = {day: NOW - timedelta(days=day, hours=1) for day in range(9)}

    def failed_file(day, lines):
        moment = days[day]
        name = f"failed-units-{moment.astimezone(timezone.utc).date()}.jsonl"
        return write_jsonl(journal / name, lines, mtime=moment)

    # The update has been failed for three days; the label watcher (a user
    # unit) only today, listed twice; yesterday nothing had failed (an empty
    # file); the day before, the older format's "no failed units" line.
    for day in (0, 1, 3, 4):
        lines = [failed_unit_line(days[day], "ibp-kiosk-update.service")]
        if day == 0:
            lines += [
                failed_unit_line(days[0], "ibp-label-watcher.service", "USER_UNIT")
            ] * 2
        if day == 1:
            lines = []
        failed_file(day, lines)
    failed_file(
        2,
        [
            {
                "__REALTIME_TIMESTAMP": usec(days[2]),
                "_HOSTNAME": "ibp-bilbo",
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "PRIORITY": "6",
                "MESSAGE": "no failed units",
            }
        ],
    )
    # Before the week: not counted, and the file isn't read.
    failed_file(8, [failed_unit_line(days[8], "ibp-kiosk-update.service")])
    _, report, _ = run(tree)
    failed = {g["message"]: g for g in report["groups"] if g["app"] == "systemd"}
    assert set(failed) == {
        "unit failed: ibp-kiosk-update.service",
        "user unit failed: ibp-label-watcher.service",
    }
    assert failed["unit failed: ibp-kiosk-update.service"]["count"] == 3
    assert failed["unit failed: ibp-kiosk-update.service"]["hosts"] == {"ibp-bilbo": 3}
    assert failed["user unit failed: ibp-label-watcher.service"]["count"] == 1
    assert report["summary"]["events"] == 4
    assert report["unreadable"] == {}


def test_station_folder_layouts_side_by_side(tree):
    station_logs(tree)  # a laptop: files in the folder itself
    write_jsonl(
        kiosk_host(tree) / "journal" / f"failed-units-{NOW.date()}.jsonl",
        [failed_unit_line(NOW - timedelta(hours=1), "ibp-kiosk-update.service")],
    )
    (tree["root"] / "IBP_KIOSK1" / "linked").symlink_to(tree["root"] / "IBP_LAPTOP")
    _, report, _ = run(tree)
    assert [s["folder"] for s in report["stations"]] == ["IBP_KIOSK1", "IBP_LAPTOP"]
    assert groups_by_app(report)["systemd"]["hosts"] == {"ibp-bilbo": 1}
    # The laptop's events are counted once: the symlink isn't followed.
    assert report["summary"]["events"] == 4


# --------------------------------------------------------------------------
# Malformed input never stops a run


def test_bad_lines_and_field_types_are_unreadable_not_fatal(tree):
    deep = "[" * 100_000 + "]" * 100_000
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(
        base / "errors-shippy.jsonl",
        [
            deep,
            "x" * (digest.MAX_LINE + 10),  # too long: skipped, not held
            event(NOW - timedelta(hours=1), msg={"not": "text"}, exc=5, level=3),
            event(NOW - timedelta(hours=1), msg=["a"], exc={"b": 1}, logger=[1]),
            json.dumps({"ts": "9999-12-31T23:59:59-12:00", "level": "ERROR"}),
            json.dumps({"ts": ["2026-11-25"], "level": "ERROR"}),
            event(NOW - timedelta(hours=2), msg="still read"),
        ],
    )
    write_jsonl(
        base / "printer-shippy.jsonl",
        [
            deep.replace("[", '[ "starting", ', 1),
            event(NOW, level="INFO", msg="shippy starting", data=["x"], version=7),
            json.dumps(
                {
                    "ts": "0001-01-01T00:00:00+05:00",
                    "msg": "shippy starting",
                    "version": "0.1.0",
                }
            ),
            shippy_starting(NOW - timedelta(days=1), "shippy", "0.2.0"),
        ],
    )
    journal = kiosk_host(tree) / "journal"
    write_jsonl(
        journal / "journal-2026-11-25.jsonl",
        [
            deep,
            '{"MESSAGE": "x", "PRIORITY": 1e400, "__REALTIME_TIMESTAMP": "'
            + usec(NOW - timedelta(minutes=1))
            + '"}',
            {"MESSAGE": "x", "PRIORITY": "3", "__REALTIME_TIMESTAMP": "9" * 30},
            {
                "MESSAGE": "x",
                "PRIORITY": "3",
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(minutes=1)),
                "SYSLOG_IDENTIFIER": ["ibp-kiosk"],
                "_HOSTNAME": {"a": 1},
            },
        ],
    )
    write_jsonl(
        journal / f"failed-units-{NOW.date()}.jsonl",
        [
            {
                "SYSLOG_IDENTIFIER": "ibp-kiosk-log-upload",
                "PRIORITY": "3",
                "MESSAGE": ["?"],
                "UNIT": {"a": 1},
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(minutes=1)),
            },
            [1, 2],
        ],
    )
    write_jsonl(
        tree["server"] / "client-errors.jsonl",
        [
            event(NOW - timedelta(hours=1), app="frontend", host=["192.0.2.1"]),
            event(NOW - timedelta(hours=1), app="frontend", host={"a": 1}),
            deep,
        ],
    )
    system = FakeSystem(
        entries=[
            {
                "MESSAGE": "x",
                "PRIORITY": 1e300,
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(minutes=1)),
            }
        ]
    )
    system.entries.append(["not", "an", "entry"])
    status, report, _ = run(tree, system=system)
    assert status == 0
    assert report["unreadable"] == {
        "IBP_KIOSK1/ibp-bilbo/journal/failed-units-2026-11-26.jsonl": 1,
        "IBP_KIOSK1/ibp-bilbo/journal/journal-2026-11-25.jsonl": 2,
        "IBP_LAPTOP/errors-shippy.jsonl": 4,
        "client-errors.jsonl": 1,
    }
    apps = groups_by_app(report)
    assert apps["frontend"]["hosts"] == {"?": 2}
    assert apps["frontend"]["count"] == 2
    shippy = [g for g in report["groups"] if g["app"] == "shippy"]
    # Level 3 isn't a level: read, but not an event.
    assert sum(g["count"] for g in shippy) == 2
    assert {g["message"] for g in shippy} >= {"still read", ""}
    # The kiosk's lines with odd field types still count, under safe names.
    assert apps["journal"]["count"] == 1
    assert apps["ibp-kiosk-log-upload"]["count"] == 1
    versions = {(v["folder"], v["app"]): v["version"] for v in report["versions"]}
    assert versions == {("IBP_LAPTOP", "shippy"): "0.2.0"}


def test_long_messages_and_stacks_are_cut_before_redaction(tree):
    message = "a" * 50_000 + " TEX-01234567-2 " + "Jane " * 1000
    assert digest.redact(message).startswith("aaa")
    assert len(digest.redact(message)) <= digest.MAX_TEXT
    cut = digest.redact("word " * 799 + "TEX-01234567-2", limit=10_000)
    assert "0123" not in cut
    stack = PY_TRACE.format(line=1, message="x") + "\n" + " " * 20_000 + "y"
    assert digest.parse_stack(stack) is not None


def test_deploy_versions_survive_any_fetch_error():
    import http.client  # pylint: disable=import-outside-toplevel

    def broken(url):
        if "/shippy/" in url:
            raise http.client.IncompleteRead(b"[proj")
        if "/printing/" in url:
            return b"\xff\xfe not toml"
        return b"project = 5\n"

    assert not digest.deploy_versions(broken)


def test_ping_survives_any_error(monkeypatch, capsys):
    import http.client  # pylint: disable=import-outside-toplevel

    def broken(*_, **__):
        raise http.client.BadStatusLine("")

    monkeypatch.setattr(digest.urllib.request, "urlopen", broken)
    digest.ping("https://hc.example/ping/abc", "/0", "body")
    assert "healthchecks ping failed: BadStatusLine" in capsys.readouterr().err


def test_server_logs_and_client_addresses(tree):
    (tree["config"] / "hosts").write_text("# address name\n192.0.2.26 ibp-pippin\n")
    write_jsonl(
        tree["server"] / "client-errors.jsonl",
        [
            event(
                NOW - timedelta(hours=5),
                msg="render failed",
                app="frontend",
                host="192.0.2.26",
                version="1.0.22",
                exc="TypeError: x is undefined\n    at f (http://192.0.2.1/assets/index-AAAAAAAA.js:1:2)",
            ),
            event(
                NOW - timedelta(hours=4),
                msg="render failed",
                app="frontend",
                host="192.0.2.99",
                version="1.0.22",
                exc="TypeError: y is undefined\n    at f (http://192.0.2.1/assets/index-AAAAAAAA.js:1:9)",
            ),
        ],
    )
    write_jsonl(
        tree["server"] / "backend.jsonl",
        [
            event(
                NOW - timedelta(hours=1),
                msg="ISBNdb timed out",
                app="backend",
                host="3f2a1b0c9d8e",
            )
        ],
    )
    write_jsonl(tree["server"] / "notes.txt", ["not a log"])
    _, report, _ = run(tree)
    apps = groups_by_app(report)
    assert apps["frontend"]["hosts"] == {"192.0.2.99": 1, "ibp-pippin": 1}
    assert apps["frontend"]["count"] == 2
    assert apps["backend"]["hosts"] == {"ibp-server": 1}


def test_pi_journal_and_failed_units(tree):
    usec = str(int((NOW - timedelta(days=1)).timestamp() * 1_000_000))
    system = FakeSystem(
        entries=[
            {
                "__REALTIME_TIMESTAMP": usec,
                "PRIORITY": "4",
                "SYSLOG_IDENTIFIER": "systemd",
                "_SYSTEMD_UNIT": "init.scope",
                "UNIT": "ibp-backup.service",
                "MESSAGE": "ibp-backup.service: Failed with result 'exit-code'.",
            }
        ],
        failed=["ibp-label-refund.service"],
    )
    status, report, _ = run(tree, system=system)
    assert status == 0
    apps = groups_by_app(report)
    assert (
        apps["ibp-backup"]["message"]
        == "ibp-backup.service: Failed with result 'exit-code'."
    )
    assert apps["ibp-backup"]["raw_event"].startswith(
        "journalctl -u ibp-backup.service "
    )
    assert apps["systemd"]["message"] == "unit failed: ibp-label-refund.service"
    assert apps["systemd"]["raw_event"] == "systemctl status ibp-label-refund.service"
    journalctl = system.calls[0]
    assert journalctl[journalctl.index("-u") + 1] == "ibp-*"
    assert journalctl[journalctl.index("-p") + 1] == "warning"


def test_unreadable_journal_fails_the_run_but_writes_the_digest(tree):
    status, report, _ = run(tree, system=FakeSystem(journal_status=1))
    assert status == 1
    assert "Pi journal: journalctl exited 1" in report["problems"]
    assert (tree["digest"] / "2026-W48.md").exists()


# --------------------------------------------------------------------------
# Week to week


def week_of_errors(tree, now, count, msg="printer BOB offline"):
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl",
        [
            event(now - timedelta(hours=i + 1), msg=msg, app="shippy")
            for i in range(count)
        ],
        mtime=now,
    )


def statuses(report):
    return [(g["status"], g["count"], g["last_week"]) for g in report["groups"]]


def test_new_recurring_growing_gone(tree):
    week = timedelta(days=7)
    week_of_errors(tree, NOW, 2)
    assert statuses(run(tree)[1]) == [("new", 2, 0)]
    week_of_errors(tree, NOW + week, 2)
    assert statuses(run(tree, now=NOW + week)[1]) == [("recurring", 2, 2)]
    week_of_errors(tree, NOW + 2 * week, 3)
    _, report, markdown = run(tree, now=NOW + 2 * week)
    assert statuses(report) == [("growing", 3, 2)]
    assert markdown.splitlines()[0].endswith(": 1 growing - run /ibp-errors")
    (tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl").unlink()
    _, report, markdown = run(tree, now=NOW + 3 * week)
    assert report["groups"] == []
    assert [(g["app"], g["last_week"]) for g in report["gone"]] == [("shippy", 3)]
    assert markdown.splitlines()[0].endswith("nothing new or growing")
    assert "## Gone" in markdown


def test_rerun_in_the_same_week_does_not_double_count(tree):
    week_of_errors(tree, NOW, 2)
    run(tree)
    _, report, _ = run(tree, now=NOW + timedelta(hours=2))
    assert statuses(report) == [("new", 2, 0)]
    seen = json.loads((tree["state"] / "seen.json").read_text())
    (entry,) = seen["fingerprints"].values()
    assert entry["weeks"] == {"2026-W48": 2}


def test_unreadable_seen_file_counts_everything_as_new(tree):
    (tree["state"] / "seen.json").write_text("{broken")
    week_of_errors(tree, NOW, 1)
    _, report, _ = run(tree)
    assert statuses(report) == [("new", 1, 0)]
    assert any("seen.json" in p for p in report["problems"])


def test_muted_groups_are_counted_not_shown(tree):
    week_of_errors(tree, NOW, 4)
    fp = run(tree, dry_run=True)[1]["groups"][0]["fingerprint"]
    (tree["config"] / "muted").write_text(
        f"# fingerprint reason\n{fp} BOB unplugged on purpose\nnot-a-fingerprint x\n"
    )
    _, report, markdown = run(tree)
    assert report["groups"] == []
    assert report["muted"] == [
        {
            "fingerprint": fp,
            "app": "shippy",
            "count": 4,
            "reason": "BOB unplugged on purpose",
        }
    ]
    assert report["summary"]["muted_events"] == 4
    assert any("not a fingerprint" in p for p in report["problems"])
    assert "## Muted" in markdown
    seen = json.loads((tree["state"] / "seen.json").read_text())
    assert seen["fingerprints"][fp]["weeks"] == {"2026-W48": 4}


def test_dry_run_writes_nothing(tree):
    week_of_errors(tree, NOW, 1)
    run(tree, dry_run=True)
    assert not tree["digest"].exists()
    assert not (tree["state"] / "seen.json").exists()


def test_old_digests_are_pruned_after_26_weeks(tree):
    tree["digest"].mkdir()
    for name in ("2026-W22.md", "2026-W22.json", "2026-W23.md", "notes.txt"):
        (tree["digest"] / name).write_text("x")
    run(tree)
    names = sorted(p.name for p in tree["digest"].iterdir())
    assert names == ["2026-W23.md", "2026-W48.json", "2026-W48.md", "notes.txt"]


def test_old_weeks_leave_seen_json(tree):
    old = {
        "app": "shippy",
        "level": "ERROR",
        "message": "m",
        "first_seen": "",
        "last_seen": "",
    }
    state = {
        "version": 1,
        "fingerprints": {
            "aaaaaaaaaaaa": {**old, "weeks": {"2026-W10": 3}},
            "bbbbbbbbbbbb": {**old, "weeks": {"2026-W10": 3, "2026-W40": 1}},
        },
    }
    (tree["state"] / "seen.json").write_text(json.dumps(state))
    run(tree)
    seen = json.loads((tree["state"] / "seen.json").read_text())["fingerprints"]
    assert list(seen) == ["bbbbbbbbbbbb"]
    assert seen["bbbbbbbbbbbb"]["weeks"] == {"2026-W40": 1}


def test_files_are_readable_by_the_reader_group_only(tree):
    group = grp.getgrgid(os.getgid()).gr_name
    run(tree, group=group)
    for name in ("2026-W48.md", "2026-W48.json"):
        info = (tree["digest"] / name).stat()
        assert info.st_mode & 0o777 == 0o640
        assert info.st_gid == os.getgid()
    assert (tree["state"] / "seen.json").stat().st_mode & 0o777 == 0o600


def test_missing_reader_group_leaves_files_root_only(tree):
    _, report, _ = run(tree, group="no-such-group-ibp")
    assert (tree["digest"] / "2026-W48.md").stat().st_mode & 0o777 == 0o600
    assert any("no-such-group-ibp" in p for p in report["problems"])


def test_release_is_read_from_ibp_tag_only(tree):
    assert digest.read_release(tree["env"]) == "v1.0.22"
    tree["env"].write_text("IBP_TAG=$(rm -rf /)\n")
    assert digest.read_release(tree["env"]) is None
    assert digest.read_release(tree["env"].parent / "missing") is None


# --------------------------------------------------------------------------
# main(): lock and Healthchecks


def main_args(tree, *extra):
    return [
        "--logs-root", str(tree["root"]),
        "--server-logs", str(tree["server"]),
        "--state-dir", str(tree["state"]),
        "--config-dir", str(tree["config"]),
        "--env-file", str(tree["env"]),
        "--group", "",
        "--no-journal",
        "--offline",
        "--now", NOW.isoformat(),
        *extra,
    ]  # fmt: skip


def test_main_without_ping_url_pings_nothing(tree, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        digest.urllib.request, "urlopen", lambda *a, **k: calls.append(a)
    )
    assert digest.main(main_args(tree)) == 0
    assert calls == []
    assert "nothing new or growing" in capsys.readouterr().out


def test_main_pings_start_and_status(tree, monkeypatch):
    (tree["config"] / "ping-url").write_text("https://hc.example/ping/abc\n")
    week_of_errors(tree, NOW, 1)
    urls = []
    monkeypatch.setattr(
        digest.urllib.request, "urlopen", lambda req, **k: urls.append(req.full_url)
    )
    assert digest.main(main_args(tree)) == 0
    assert urls == [
        "https://hc.example/ping/abc/start",
        "https://hc.example/ping/abc/0",
    ]


def test_main_refuses_a_second_run(tree):
    lock = digest.take_lock(tree["state"] / digest.LOCK_NAME)
    try:
        assert digest.main(main_args(tree)) == digest.BUSY
    finally:
        lock.close()


def test_main_dry_run_prints_the_digest(tree, capsys):
    week_of_errors(tree, NOW, 1)
    assert digest.main(main_args(tree, "--dry-run")) == 0
    assert capsys.readouterr().out.startswith("# IBP error digest 2026-W48: 1 new")
    assert not tree["digest"].exists()


# --------------------------------------------------------------------------
# Raw events: only safe paths, read with --show-raw


def show_raw(tree, raw, capsys):
    status = digest.main(
        [
            "--logs-root", str(tree["root"]),
            "--server-logs", str(tree["server"]),
            "--show-raw", raw,
        ]
    )  # fmt: skip
    out = capsys.readouterr()
    return status, out.out, out.err


def test_raw_event_is_named_only_for_a_plain_path(tree):
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(base / "errors-shippy.jsonl", [event(NOW - timedelta(hours=1))])
    odd = "errors-x'; touch pwned; echo '.jsonl"
    write_jsonl(
        base / odd, [event(NOW - timedelta(hours=1), msg="from an odd file"), "{bad"]
    )
    _, report, markdown = run(tree)
    by_msg = {g["message"]: g for g in report["groups"]}
    assert by_msg["label purchase failed"]["raw_event"] == (
        f"{base / 'errors-shippy.jsonl'}:1"
    )
    # Still counted, but with no path, and the name is never shown.
    assert by_msg["from an odd file"]["raw_event"] is None
    assert by_msg["from an odd file"]["app"] == "unknown"
    assert report["unreadable"] == {"IBP_LAPTOP/<unusual name>": 1}
    assert "pwned" not in markdown and "pwned" not in json.dumps(report)


def test_show_raw_prints_one_event_without_data(tree, capsys):
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(
        base / "errors-shippy.jsonl",
        [
            event(NOW - timedelta(hours=2), msg="first"),
            event(NOW - timedelta(hours=1), msg="second", data={"name": "Jane"}),
        ],
    )
    _, report, _ = run(tree)
    raw = {g["message"]: g["raw_event"] for g in report["groups"]}["second"]
    status, out, _ = show_raw(tree, raw, capsys)
    assert status == 0
    shown = json.loads(out)
    assert shown["msg"] == "second"
    assert "data" not in shown and "Jane" not in out
    assert set(shown) <= set(digest.RAW_FIELDS)
    status, out, _ = show_raw(tree, raw.rsplit(":", 1)[0] + ":9", capsys)
    assert (status, out) == (1, "")


def test_show_raw_prints_a_journal_entry(tree, capsys):
    path = write_jsonl(
        kiosk_host(tree) / "journal" / "journal-2026-11-25.jsonl",
        [
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(hours=1)),
                "PRIORITY": "3",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "MESSAGE": "tile failed",
                "_CMDLINE": "python3 -m ibp_kiosk --secret",
            }
        ],
    )
    status, out, _ = show_raw(tree, f"{path}:1", capsys)
    assert status == 0
    assert json.loads(out)["MESSAGE"] == "tile failed"
    assert "_CMDLINE" not in out


@pytest.mark.parametrize(
    "suffix",
    [
        "IBP_LAPTOP/../IBP_LAPTOP/errors-shippy.jsonl:1",
        "IBP_LAPTOP/./errors-shippy.jsonl:1",
        "IBP_LAPTOP//errors-shippy.jsonl:1",
        "IBP_LAPTOP/.hidden.jsonl:1",
        "IBP_LAPTOP/errors-shippy.jsonl:0",
        "IBP_LAPTOP/errors-shippy.jsonl:x",
        "IBP_LAPTOP/errors-shippy.jsonl",
        "IBP_LAPTOP/errors shippy.jsonl:1",
        "IBP_LAPTOP/errors-'shippy.jsonl:1",
    ],
)
def test_show_raw_refuses_any_other_path(tree, capsys, suffix):
    write_jsonl(tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl", [event(NOW)])
    status, out, err = show_raw(tree, f"{tree['root']}/{suffix}", capsys)
    assert (status, out) == (2, "") and "--show-raw" in err


def test_show_raw_refuses_paths_outside_the_log_folders(tree, tmp_path, capsys):
    outside = write_jsonl(tmp_path / "elsewhere" / "x.jsonl", [event(NOW)])
    for raw in (f"{outside}:1", "/etc/shadow:1", "relative/x.jsonl:1"):
        assert show_raw(tree, raw, capsys)[:2] == (2, "")


def test_show_raw_never_follows_a_symlink_or_opens_a_fifo(tree, tmp_path, capsys):
    secret = write_jsonl(tmp_path / "secret" / "x.jsonl", [event(NOW, msg="secret")])
    base = tree["root"] / "IBP_LAPTOP"
    base.mkdir()
    (base / "errors-link.jsonl").symlink_to(secret)
    (tree["root"] / "linked").symlink_to(secret.parent, target_is_directory=True)
    os.mkfifo(base / "errors-fifo.jsonl")
    for raw in (
        f"{base}/errors-link.jsonl:1",
        f"{tree['root']}/linked/x.jsonl:1",
        f"{base}/errors-fifo.jsonl:1",
    ):
        status, out, _ = show_raw(tree, raw, capsys)
        assert (status, out) == (1, ""), raw


def test_open_beneath_walks_each_component_without_symlinks(tree, tmp_path):
    real = write_jsonl(tmp_path / "real" / "sub" / "x.jsonl", [event(NOW)])
    (tree["root"] / "sub").symlink_to(real.parent, target_is_directory=True)
    with pytest.raises(OSError):
        digest.open_beneath(tree["root"], ["sub", "x.jsonl"])
    os.close(digest.open_beneath(tmp_path / "real", ["sub", "x.jsonl"]))
    with pytest.raises(OSError):
        digest.open_beneath(tmp_path / "real", ["sub"])  # a folder
    with pytest.raises(OSError):
        digest.open_beneath(tmp_path / "real", ["..", "real", "sub", "x.jsonl"])


# --------------------------------------------------------------------------
# Secrets


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "header Authorization: Bearer abcdef0123456789abcdef0123",
            "header Authorization: Bearer <secret>",
        ),
        ("call with api_key=sk-ant-0123abcd&q=1 failed", "call with api_key=<secret>&q=1 failed"),
        ("token: ghp_Ab12Cd34Ef56 rejected", "token: <secret> rejected"),
        ("password=hunter2 refused", "password=<secret> refused"),
        ("GET https://maps.example/geo?lang=en&key=AIzaFAKE1234 403",
         "GET https://maps.example/geo?lang=en&key=<secret> 403"),
        ("got https://user:pw@hc.example/ping", "got https://<secret>@hc.example/ping"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2",
         "jwt <secret>"),
        ("id 32b5a86d-ba4a-4756-a001-b51000629787 gone", "id <secret> gone"),
        ("KeyError: isbn", "KeyError: isbn"),
        ("shippy.gui.workers.shipment_worker failed", "shippy.gui.workers.shipment_worker failed"),
    ],
)  # fmt: skip
def test_redact_secrets(text, expected):
    assert digest.redact(text) == expected


SECRET = "sk-ant-api03-Zq9fake0secret0VALUE0x7Hj2"


def test_a_secret_never_reaches_the_digest_or_the_state(tree):
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(
        base / "errors-shippy.jsonl",
        [
            event(NOW - timedelta(hours=1), msg=f"EasyPost said key={SECRET}"),
            event(
                NOW - timedelta(hours=2),
                exc=PY_TRACE.format(line=41, message=f"401 for Bearer {SECRET}"),
            ),
            event(NOW - timedelta(hours=3), msg=f"posting {SECRET} failed"),
        ],
    )
    write_jsonl(
        kiosk_host(tree) / "journal" / "journal-2026-11-25.jsonl",
        [
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(hours=1)),
                "PRIORITY": "3",
                "SYSLOG_IDENTIFIER": "ibp-kiosk",
                "MESSAGE": f"login with token={SECRET} refused",
            }
        ],
    )
    _, report, _ = run(tree)
    assert report["summary"]["events"] == 4
    for name in ("2026-W48.md", "2026-W48.json"):
        assert SECRET not in (tree["digest"] / name).read_text()
    seen = (tree["state"] / "seen.json").read_text()
    assert SECRET not in seen
    for entry in json.loads(seen)["fingerprints"].values():
        assert set(entry) == {"app", "level", "first_seen", "last_seen", "weeks"}


def test_seen_json_drops_old_message_text(tree):
    old = {
        "app": "shippy",
        "level": "ERROR",
        "message": "an old message with a name in it",
        "first_seen": "2026-11-10T10:00:00-06:00",
        "last_seen": "2026-11-18T10:00:00-06:00",
        "weeks": {"2026-W47": 2},
    }
    state = {"version": 1, "fingerprints": {"aaaaaaaaaaaa": old}}
    (tree["state"] / "seen.json").write_text(json.dumps(state))
    _, report, markdown = run(tree)
    assert report["gone"] == [
        {
            "app": "shippy",
            "level": "ERROR",
            "last_seen": "2026-11-18T10:00:00-06:00",
            "fingerprint": "aaaaaaaaaaaa",
            "last_week": 2,
        }
    ]
    assert "old message" not in markdown
    seen = (tree["state"] / "seen.json").read_text()
    assert "message" not in seen and "aaaaaaaaaaaa" in seen


# --------------------------------------------------------------------------
# Unreadable lines and files


def test_a_lone_surrogate_is_an_unreadable_line_not_a_crash(tree):
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl",
        [
            '{"ts": "2026-11-26T08:00:00-06:00", "level": "ERROR", "msg": "x \\ud800"}',
            '{"ts": "2026-11-26T08:00:00-06:00", "level": "ERROR", "msg": "ok \\ud83d\\ude00"}',
        ],
    )
    system = FakeSystem(
        entries=[
            {
                "__REALTIME_TIMESTAMP": usec(NOW - timedelta(hours=1)),
                "PRIORITY": "3",
                "_SYSTEMD_UNIT": "ibp-backup.service",
                "MESSAGE": "\ud800",
            }
        ]
    )
    system.entries = [json.dumps(e) for e in system.entries]
    status, report, markdown = run(tree, system=_RawSystem(system.entries))
    assert status == 0
    assert report["unreadable"] == {
        "IBP_LAPTOP/errors-shippy.jsonl": 1,
        "Pi journal": 1,
    }
    assert [g["message"] for g in report["groups"]] == ["ok \U0001f600"]
    assert (tree["digest"] / "2026-W48.md").read_text() == markdown + "\n"


class _RawSystem(FakeSystem):
    """journalctl printing these lines as they are."""

    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def __call__(self, argv):
        if argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.lines), "")
        return super().__call__(argv)


def unopenable(monkeypatch, name):
    real = digest.open_beneath

    def open_beneath(root, parts):
        if parts[-1] == name:
            raise PermissionError("no")
        return real(root, parts)

    monkeypatch.setattr(digest, "open_beneath", open_beneath)


def test_an_unreadable_file_marks_the_digest_and_keeps_seen_json(tree, monkeypatch):
    week = timedelta(days=7)
    week_of_errors(tree, NOW - week, 2)
    run(tree, now=NOW - week)
    before = (tree["state"] / "seen.json").read_text()
    week_of_errors(tree, NOW, 2)
    unopenable(monkeypatch, "errors-shippy.jsonl")
    status, report, markdown = run(tree)
    assert status == 1
    assert report["complete"] is False
    assert markdown.splitlines()[0].startswith(
        "# IBP error digest 2026-W48 (INCOMPLETE: a source could not be read)"
    )
    assert "IBP_LAPTOP/errors-shippy.jsonl: could not be opened" in report["problems"]
    assert "## Not seen this week (a source could not be read: may not be gone)" in (
        markdown
    )
    assert (tree["digest"] / "2026-W48.md").exists()
    assert (tree["state"] / "seen.json").read_text() == before


def test_main_pings_failure_when_a_file_is_unreadable(tree, monkeypatch):
    (tree["config"] / "ping-url").write_text("https://hc.example/ping/abc\n")
    week_of_errors(tree, NOW, 1)
    unopenable(monkeypatch, "errors-shippy.jsonl")
    urls = []
    monkeypatch.setattr(
        digest.urllib.request, "urlopen", lambda req, **k: urls.append(req.full_url)
    )
    assert digest.main(main_args(tree)) == 1
    assert urls[-1] == "https://hc.example/ping/abc/1"


# --------------------------------------------------------------------------
# Versions with a commit


def test_versions_compare_the_base_and_list_the_weeks_commits(tree):
    base = tree["root"] / "IBP_LAPTOP"
    write_jsonl(
        base / "printer-shippy.jsonl",
        [
            shippy_starting(NOW - timedelta(days=10), "shippy", "0.2.0+g1111111"),
            shippy_starting(NOW - timedelta(days=3), "shippy", "0.2.0+g9a886e3"),
            shippy_starting(NOW - timedelta(days=2), "shippy", "0.2.0+gb8ac3af"),
        ],
    )
    write_jsonl(
        base / "printer-shippy-gui.jsonl",
        [shippy_starting(NOW - timedelta(days=1), "shippy-gui", "0.1.0+g9a886e3")],
    )
    _, report, markdown = run(tree)
    versions = {v["app"]: v for v in report["versions"]}
    assert versions["shippy"]["status"] == "current"
    assert versions["shippy"]["version"] == "0.2.0+gb8ac3af"
    assert versions["shippy"]["commits"] == ["9a886e3", "b8ac3af"]
    assert versions["shippy-gui"]["status"] == "old"
    assert "| 9a886e3, b8ac3af |" in markdown
    assert digest.base_version("v0.2.0+g9a886e3.d20261128") == "0.2.0"
    assert digest.version_commit("0.2.0+g9a886e3.d20261128") == "9a886e3"
    assert digest.version_commit("0.2.0") is None


# --------------------------------------------------------------------------
# Kiosk journal replays


def test_journal_entries_exported_twice_count_once(tree):
    def entry(cursor, hours):
        return {
            "__CURSOR": cursor,
            "__REALTIME_TIMESTAMP": usec(NOW - timedelta(hours=hours)),
            "PRIORITY": "3",
            "SYSLOG_IDENTIFIER": "ibp-kiosk",
            "MESSAGE": "tile failed",
        }

    for host in ("ibp-bilbo", "ibp-frodo"):
        journal = kiosk_host(tree, host=host) / "journal"
        write_jsonl(journal / "journal-2026-11-25.jsonl", [entry("s=a;i=1", 30)])
        # The upload stopped after the rename: the next run exported it again.
        write_jsonl(
            journal / "journal-2026-11-26.jsonl",
            [entry("s=a;i=1", 30), entry("s=a;i=2", 2), entry("", 1), entry("", 1)],
        )
    _, report, _ = run(tree)
    (group,) = report["groups"]
    # Per host: i=1 once, i=2, and the two entries with no cursor.
    assert group["hosts"] == {"ibp-bilbo": 4, "ibp-frodo": 4}


# --------------------------------------------------------------------------
# Weekly windows and station freshness


def chicago(*args):
    return datetime(*args, tzinfo=digest.TZ)


@pytest.mark.parametrize(
    "now, since, until",
    [
        # On time, a little late, a day late (Persistent=), the day before.
        (chicago(2026, 11, 26, 9), chicago(2026, 11, 19, 9), chicago(2026, 11, 26, 9)),
        (chicago(2026, 11, 26, 9, 3), chicago(2026, 11, 19, 9), chicago(2026, 11, 26, 9)),
        (chicago(2026, 11, 27, 12), chicago(2026, 11, 19, 9), chicago(2026, 11, 26, 9)),
        (chicago(2026, 11, 26, 8, 59), chicago(2026, 11, 12, 9), chicago(2026, 11, 19, 9)),
        # Across the fall DST change (Nov 1): 7 days and an hour.
        (chicago(2026, 11, 5, 9), chicago(2026, 10, 29, 9), chicago(2026, 11, 5, 9)),
    ],
)  # fmt: skip
def test_week_window_is_anchored_to_thursday_9am_chicago(now, since, until):
    assert digest.week_window(now) == (
        since.astimezone(timezone.utc),
        until.astimezone(timezone.utc),
    )


def test_consecutive_windows_meet_across_dst():
    run_time = chicago(2026, 10, 1, 9, 30)
    for _ in range(12):
        since, until = digest.week_window(run_time)
        _, next_until = digest.week_window(run_time + timedelta(days=7))
        assert digest.week_window(run_time + timedelta(days=7))[0] == until
        assert since < until < next_until
        run_time += timedelta(days=7)


def test_a_late_run_reads_the_scheduled_week_and_no_more(tree):
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl",
        [
            event(NOW - timedelta(days=7, minutes=1), msg="the week before"),
            event(NOW - timedelta(days=7), msg="first of the week"),
            event(NOW - timedelta(minutes=1), msg="last of the week"),
            event(NOW, msg="next week"),
            event(NOW + timedelta(hours=20), msg="next week too"),
        ],
        mtime=NOW + timedelta(days=1),
    )
    _, report, _ = run(tree, now=NOW + timedelta(days=1, hours=2))
    assert report["week"] == "2026-W48"
    assert sorted(g["message"] for g in report["groups"]) == [
        "first of the week",
        "last of the week",
    ]
    assert report["window"] == {
        "since": "2026-11-19T09:00:00-06:00",
        "until": "2026-11-26T09:00:00-06:00",
    }
    assert report["generated"] == "2026-11-27T11:00:00-06:00"


def test_the_dst_hour_is_read_by_the_next_digest(tree):
    gap = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)  # 09:30 CDT
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "errors-shippy.jsonl",
        [event(gap, msg="in the extra hour")],
        mtime=gap,
    )
    _, report, _ = run(tree, now=chicago(2026, 11, 5, 9))
    assert [g["message"] for g in report["groups"]] == ["in the extra hour"]


def test_freshness_is_when_the_file_changed_here_not_its_mtime(
    tree, ctime_is_mtime, monkeypatch
):
    monkeypatch.setattr(digest, "file_times", ctime_is_mtime)
    now = datetime.now(timezone.utc)
    # Uploaded just now, but the station last wrote it a month ago.
    write_jsonl(
        tree["root"] / "IBP_LAPTOP" / "printer-shippy.jsonl",
        [shippy_starting(now - timedelta(days=30), "shippy", "0.2.0")],
        mtime=now - timedelta(days=30),
    )
    _, report, _ = run(tree, now=now)
    (station,) = report["stations"]
    assert station["stale"] is False
    assert station["last_upload"] >= digest.iso(now - timedelta(minutes=5))


# --------------------------------------------------------------------------
# The kiosk launcher: one journal entry per record, traceback included

LAUNCHER_TRACE = (
    "ibp-kiosk-launcher: ERROR tile {tile} failed to start\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/lib/python3/dist-packages/ibp_kiosk/app.py", line {line}, in launch\n'
    "    self.start(tile)\n"
    '  File "/usr/lib/python3/dist-packages/ibp_kiosk/tiles.py", line 52, in start\n'
    "    raise FileNotFoundError(path)\n"
    "FileNotFoundError: [Errno 2] No such file: '/opt/{tile}'"
)


def launcher_entry(moment, message, **extra):
    return {
        "__REALTIME_TIMESTAMP": usec(moment),
        "PRIORITY": "3",
        "SYSLOG_IDENTIFIER": "ibp-kiosk",
        "_HOSTNAME": "ibp-bilbo",
        "MESSAGE": message,
        "CODE_FILE": "/usr/lib/python3/dist-packages/ibp_kiosk/app.py",
        "CODE_LINE": "140",
        "CODE_FUNC": "launch",
        **extra,
    }


def test_launcher_tracebacks_are_one_event_with_frames(tree):
    cut = LAUNCHER_TRACE.format(tile="b", line=91).split("\n")
    cut = "\n".join([cut[0], "[3 lines left out]", *cut[-3:]])
    write_jsonl(
        kiosk_host(tree) / "journal" / "journal-2026-11-25.jsonl",
        [
            launcher_entry(
                NOW - timedelta(hours=3), LAUNCHER_TRACE.format(tile="a", line=90)
            ),
            launcher_entry(NOW - timedelta(hours=2), cut),
            launcher_entry(
                NOW - timedelta(hours=1),
                "ibp-kiosk-launcher: WARNING portal slow (12 s)",
                CODE_FUNC="watch",
            ),
            launcher_entry(
                NOW - timedelta(minutes=30),
                "ibp-kiosk-launcher: WARNING portal slow (30 s)",
                CODE_FUNC="watch",
            ),
        ],
    )
    _, report, _ = run(tree)
    by_count = sorted(report["groups"], key=lambda g: g["message"])
    tile, slow = by_count
    # The same failure, whole or cut, and whatever the tile: one group.
    assert tile["count"] == 2
    assert tile["exception"]["type"] == "FileNotFoundError"
    assert tile["frames"][-1] == "ibp_kiosk/tiles.py:52 start"
    assert tile["message"].startswith("ibp-kiosk-launcher: ERROR tile")
    assert "Traceback" not in tile["message"]
    # No traceback: grouped by CODE_FILE and CODE_FUNC.
    assert slow["count"] == 2
    assert slow["frames"] == ["ibp_kiosk/app.py:140 watch"]
    assert len(report["groups"]) == 2
