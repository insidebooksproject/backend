"""The shared error log format: the backend's own ``backend.jsonl`` and the
browser reports' ``client-errors.jsonl``.

Both files hold one JSON object per line, the event format every IBP app
writes (see ``docs/error-reporting.md`` in the meta repo, and
``ibp_printing.log.JsonFormatter``, whose fields these follow)::

    {"ts": "2026-11-26T14:03:12.481-06:00", "level": "ERROR",
     "app": "backend", "version": "v1.0.22", "logger": "ibp.api",
     "host": "ibp-server", "msg": "...", "exc": "Traceback ...", "data": {}}

They are written to the log directory, ``[logging] log_dir`` (the
``IBP_LOG_DIR`` environment variable; ``/logs`` in the image, mounted from
``/srv/ibp/logs`` on the Pi). Each file rotates at 5 MiB and keeps 4 old
copies. The files are made ``0640`` (rotated copies keep it) and a missing
directory ``0750``, as they can hold inmate data. If the directory can't be
written, the backend runs without the files and says so once on stderr.
"""

import datetime
import json
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

APP = "backend"
BACKEND_LOG_NAME = "backend.jsonl"
CLIENT_LOG_NAME = "client-errors.jsonl"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 4
DEFAULT_LOG_DIR = "logs"
LOG_FILE_MODE = 0o640
LOG_DIR_MODE = 0o750

logger = logging.getLogger(__name__)


def get_version() -> str:
    """The running release: ``IBP_VERSION``, set when the image is built
    (``vX.Y.Z`` for a release, ``sha-<commit>`` otherwise), or ``dev``."""
    return os.environ.get("IBP_VERSION", "").strip() or "dev"


def get_host() -> str:
    """The name the backend's own events carry: ``IBP_LOG_HOST`` (the Pi's
    compose file sets ``ibp-server``), else the container's host name."""
    return os.environ.get("IBP_LOG_HOST", "").strip() or socket.gethostname()


def get_log_dir() -> Path:
    """The configured log directory.

    A relative path is resolved against the directory of the config file
    (the project toplevel for ``sample.conf``), as ``[logging] logfile`` is.
    """
    # pylint: disable=import-outside-toplevel,cyclic-import
    from .base import config, get_toplevel_path

    value = config.get("logging", "log_dir", fallback=DEFAULT_LOG_DIR).strip()
    path = Path(value or DEFAULT_LOG_DIR).expanduser()
    if not path.is_absolute():
        base_dir = config.path.parent if config.path else get_toplevel_path()
        path = base_dir / path
    return path


def timestamp(created: float) -> str:
    """Local time with its UTC offset: ``2026-11-26T14:03:12.481-06:00``."""
    moment = datetime.datetime.fromtimestamp(created).astimezone()
    return moment.isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """One event per record, in the shared format, for the backend's own log.

    A record's ``data`` (``extra={"data": {...}}``) is copied when it is a
    dict; ``exc`` is the formatted traceback.
    """

    def __init__(self, app: str = APP, version: str | None = None, host: str = ""):
        super().__init__()
        self.app = app
        self.version = version or get_version()
        self.host = host or get_host()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": timestamp(record.created),
            "level": record.levelname,
            "app": self.app,
            "version": self.version,
            "logger": record.name,
            "host": self.host,
            "pid": record.process,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=repr, ensure_ascii=False)


class EventFormatter(logging.Formatter):
    """Writes a record's ``event`` dict as it is (the client reports)."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(getattr(record, "event"), default=repr, ensure_ascii=False)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """A ``RotatingFileHandler`` whose file is ``LOG_FILE_MODE`` (0640).

    Each file it opens, the first and each one after a rotation, is created
    with that mode whatever the umask, and an existing file it opens is set
    to it (if it is the owner); a rotated copy is renamed, so keeps it.
    """

    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.baseFilename, flags, LOG_FILE_MODE)
        try:
            os.fchmod(fd, LOG_FILE_MODE)
        except OSError:  # not the file's owner: leave its mode
            pass
        # pylint: disable-next=unspecified-encoding
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


def open_log_file(
    name: str, handler_class: type[RotatingFileHandler] = PrivateRotatingFileHandler
) -> RotatingFileHandler:
    """A size-rotated handler for ``name`` in the log directory, its file
    ``0640`` (a missing directory is made ``0750``).

    Raises ``OSError`` if the directory can't be made or the file opened.
    """
    log_dir = get_log_dir()
    log_dir.mkdir(mode=LOG_DIR_MODE, parents=True, exist_ok=True)
    return handler_class(
        log_dir / name,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )


def unavailable_message(name: str, error: OSError) -> str:
    """The one stderr line saying a log file is not being written."""
    return (
        f"Not writing {name}: the log directory {get_log_dir()} can't be "
        f"written ({error}); set IBP_LOG_DIR or [logging] log_dir to one "
        "that can"
    )


def build_backend_handler() -> tuple[logging.Handler | None, str | None]:
    """The handler writing the backend's WARNING and above to
    ``backend.jsonl``, or ``None`` and why not."""
    try:
        handler = open_log_file(BACKEND_LOG_NAME)
    except OSError as error:
        return None, unavailable_message(BACKEND_LOG_NAME, error)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(JsonFormatter())
    return handler, None


class WriteFailed(Exception):
    """A report couldn't be appended (a full disk, a failed rotation)."""


class _ObservedFileHandler(PrivateRotatingFileHandler):
    """Keeps the error an ``emit`` failed with, instead of printing it."""

    failure: BaseException | None = None

    def handleError(self, record: logging.LogRecord) -> None:
        self.failure = sys.exc_info()[1]


class _ObservedStreamHandler(logging.StreamHandler):
    """Keeps the error an ``emit`` failed with, instead of printing it."""

    failure: BaseException | None = None

    def handleError(self, record: logging.LogRecord) -> None:
        self.failure = sys.exc_info()[1]


class ClientErrorLog:
    """Appends browser reports to ``client-errors.jsonl``.

    Opened on first use (or by :meth:`open` at startup). When the file
    can't be opened, reports go to stderr instead, so they are at least in
    ``docker compose logs``, and the failure is said once. The handler is
    called directly, not through a logger, so no logging configuration
    (levels, disabled loggers) can drop a report. A write that fails once
    the file is open (a full disk, a failed rotation) raises
    :class:`WriteFailed`; the first of a run of failures is logged.
    """

    def __init__(self) -> None:
        self._handler: _ObservedFileHandler | _ObservedStreamHandler | None = None
        self._lock = threading.Lock()
        self._failing = False

    def open(self) -> logging.Handler:
        """Open the file, or fall back to stderr; once."""
        with self._lock:
            return self._open()

    def _open(self) -> _ObservedFileHandler | _ObservedStreamHandler:
        if self._handler is not None:
            return self._handler
        handler: _ObservedFileHandler | _ObservedStreamHandler
        try:
            handler = cast(
                _ObservedFileHandler,
                open_log_file(CLIENT_LOG_NAME, _ObservedFileHandler),
            )
        except OSError as error:
            logger.warning("%s", unavailable_message(CLIENT_LOG_NAME, error))
            handler = _ObservedStreamHandler()
        handler.setFormatter(EventFormatter())
        self._handler = handler
        return handler

    def close(self) -> None:
        """Close the file; the next write opens it again."""
        with self._lock:
            if self._handler is not None:
                self._handler.close()
                self._handler = None
            self._failing = False

    def write(self, event: dict[str, Any]) -> None:
        """Append one event; raises :class:`WriteFailed` if it wasn't."""
        record = logging.makeLogRecord({"msg": "client error", "event": event})
        with self._lock:
            handler = self._open()
            handler.failure = None
            handler.handle(record)
            failure, handler.failure = handler.failure, None
            if failure is None:
                self._failing = False
                return
            if not self._failing:
                self._failing = True
                logger.warning(
                    "Not writing %s: %s; reports are refused (503) until it "
                    "can be written",
                    CLIENT_LOG_NAME,
                    describe_write_failure(failure),
                )
        raise WriteFailed(type(failure).__name__) from failure


def describe_write_failure(error: BaseException) -> str:
    """A write failure without the event's text: an ``OSError``'s errno
    message (``[Errno 28] No space left on device``), else the class name."""
    if isinstance(error, OSError) and error.strerror:
        return f"[Errno {error.errno}] {error.strerror}"
    return type(error).__name__


client_error_log = ClientErrorLog()
