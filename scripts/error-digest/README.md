# Error digest

Weekly job on ibp-server that sums up the week's warnings and errors from
every IBP app in one short, redacted report, for an AI session to read
(the meta repo's `/ibp-errors` skill). The design, and the format the apps
write, is the meta repo's `docs/error-reporting.md`. It only reads logs: it
changes nothing but its own digest and state folders.

- `ibp-error-digest`: reads the week that ended at the latest scheduled
  run, groups the events, compares them with earlier weeks and writes
  `/srv/ibp-logs/digest/<YYYY>-W<ww>.md` and a `.json` copy. With
  `--show-raw PATH:LINE` it prints one raw event instead (below). Python 3
  stdlib only.
- `ibp-error-digest.service` / `.timer`: Thursdays 09:00 America/Chicago,
  `Persistent=true`, the day after the label refunds. Runs as **root**,
  because `jstarr` can't read the stations' folders, with
  `ProtectSystem=strict`: the only paths it can write are
  `/srv/ibp-logs/digest` and its state folder `/var/lib/ibp-error-digest`.

## What it reads

Each station's Samba share is its own folder, `/srv/ibp-logs/<station>/`.
A Windows laptop uploads into the folder itself (`IBP_LAPTOP/errors-shippy.jsonl`);
a kiosk into a folder named after its host (`uname -n`), with the print
server's logs and its journal export below it (kiosk `docs/apps.md`, "Log
upload"). Events from a laptop are shown as from the station folder, a
kiosk's as from its host.

| Source | Format |
|---|---|
| `<station>/errors-*.jsonl` (and rotated `.1`...), and a kiosk's `<station>/<host>/errors-*.jsonl` and `<station>/<host>/print-server/errors-*.jsonl` | the shared JSONL format (`ts`, `level`, `app`, `version`, `host`, `logger`, `msg`, `exc`, `data`) |
| `<station>/<host>/journal/journal-<day>.jsonl` | a kiosk's journal entries at warning or above, in journald's field names (`__REALTIME_TIMESTAMP`, `PRIORITY`, `MESSAGE`, `SYSLOG_IDENTIFIER`, `_SYSTEMD_UNIT`, `UNIT`, `CODE_FILE`, `CODE_FUNC`, ...). An entry exported twice (an upload stopped between its rename and its cursor save) has the same `__CURSOR` and counts once per station and host. `MESSAGE`'s first line is the message; a traceback after it (the launcher writes a record and its traceback as one entry, cut to the first and last lines) becomes the stack, else a Python logger's `CODE_FILE` and `CODE_FUNC` are its one frame |
| `<station>/<host>/journal/failed-units-<day>.jsonl` | the units failed at the kiosk's last upload that day (UTC), rewritten at every upload; empty or missing when none is. One journal-shaped line per unit: `SYSLOG_IDENTIFIER` `ibp-kiosk-log-upload`, `PRIORITY` `3`, `MESSAGE` `<unit> failed`, `UNIT` (system) or `USER_UNIT` (the kiosk user's), `__REALTIME_TIMESTAMP`. Each becomes one event a day, app `systemd`, `unit failed: <unit>` or `user unit failed: <unit>`, so a unit that stays failed counts every day the station runs |
| the same folders' `{printer,errors}-*.jsonl` | only the `<app> starting` lines (`shippy starting`, `label watcher starting`), and of those only `ts`, `app`, `host` and the version (`version`, or `data.version`), for each station's (a kiosk: each host's) app versions. The print server's starting line has no version |
| `/srv/ibp/logs/*.jsonl` | `backend.jsonl` (host `ibp-server`) and `client-errors.jsonl` (host: the client address, named by `/etc/ibp-error-digest/hosts`) |
| `journalctl -u 'ibp-*' -p warning` | the Pi's own jobs (backup, refunds, sync, this one), and `systemctl --failed` |

The week is half-open, from the Thursday 09:00 America/Chicago before last
to the latest one at or before the run (`since <= ts < until`): a run late
under `Persistent=`, a re-run before the next Thursday, or a DST change
reads the same week, so consecutive digests meet with no gap. Events below
WARNING are skipped. A line that isn't JSON, is over 256 KB,
is nested too deep to parse, holds text that can't be written as UTF-8 (an
escaped lone surrogate), isn't an event (no time stamp, or one outside
2000-2100), or has a field of a type that trips the reader, is counted as
unreadable, by file, in the report; no line stops a run. A file or folder
that can't be opened, listed or read to the end, or a missing log folder, makes the run
**incomplete**: the digest is still written, its first line says
`(INCOMPLETE: a source could not be read)` and `complete` is `false` in the
`.json`, the gone groups are listed as "not seen, may not be gone",
`seen.json` is left as it was (so the week's partial counts don't make
groups gone now or growing next week), and the exit status is 1, so
Healthchecks gets a failure. Only the first
4000 characters of a message are redacted (and at most 300 kept), and a
stack's lines are cut to 1000 characters. A missing `/srv/ibp-logs` or `/srv/ibp/logs`
(which exists from backend v1.0.22) makes the run incomplete too. Files are opened one
path component at a time below their log folder without following a
symlink in any of them, and only if they are regular files; a file not
modified since a day before the week began is not read for events (its
lines are older). `digest/` is not a station. A file whose name isn't made
of plain characters (`A-Z a-z 0-9 . _ @ + -`, not starting with a dot) is
still read, but the report names it `<unusual name>` and gives its events
no raw path.

## What it writes

1. **Groups** by fingerprint (12 hex digits): app, exception type and the
   innermost frame in IBP code (`file:function`, so a line number or a
   message changing doesn't make a new group), or, for an event with no
   exception, its redacted message with numbers and codes replaced. The
   browser apps (`frontend`, `book-portal-extension`) are grouped by their
   `logger` (where the error was reported), `data.operation` (the API call,
   e.g. the extension's `api.books.get`, shown as `operation`),
   `data.status` (the HTTP status, shown as `http_status`), exception type
   and message shape instead: every
   API call fails in the same response helper, and the frontend's frames
   are minified (their function names are left out of the stack shown).
2. **Status**, from `/var/lib/ibp-error-digest/seen.json` (fingerprint ->
   app, level, first and last seen, weekly counts for 26 weeks; no message
   text): **new** (no earlier week), **growing** (up by half or more on
   last week), **recurring**, or **gone** (last week, not this week).
   Running again in the same week replaces that week's counts.
3. **Muted** groups, `/etc/ibp-error-digest/muted` (`<fingerprint> <reason>`
   per line, `#` comments), are counted and listed with their reason but
   left out of the groups and statuses.
4. **Stations**: each folder's newest file change time on the Pi (ctime,
   which a copy can't preserve, so this is the last upload of a changed
   file; the mtime is the station's) and **no upload all week** when it is
   older than the week. A station whose uploader skips unchanged files and
   that logged nothing all week still looks stale. Each station app's
   version from its latest `starting` line, next to the `deploy` branch's
   (`shippy`, `printing`, `kiosk`, read from GitHub; `unknown` when it
   can't be) or the release's `IBP_TAG` (backend, frontend, extension).
   Only the base version is compared (`0.1.0+g9a886e3` is `0.1.0`), so it
   is `old` only when the package version is behind; `commits` lists the
   commits (`+g<sha>`) of the week's starts, to compare with the `deploy`
   branch's commit (`/ibp-errors` does).
5. **The report**'s first line says whether there are new or growing
   groups. Each group has its status, app, highest level, count, hosts,
   versions, first and last seen, last week's count, the redacted message
   and exception line, the logger, the stack as frames only (`file:line
   function`, the last 12, no paths above the package, no source lines or
   locals), and where one raw event is (`raw_event`): `path:line` on the
   Pi, for `--show-raw`, a `journalctl` or `systemctl` command, or `null`
   for a file with an unusual name. Digests older than 26 weeks are
   removed.

**Redaction.** No `data` field (but a browser app's numeric `status` and
plain-name `operation`),
request body or raw line is ever copied. In each message and exception
line: secrets become `<secret>` (a URL's `user:password@`, `Bearer`/`Basic`
credentials, the value of `key=`, `token=`, `password:` and the like, JWTs,
runs of 24 or more of `A-Z a-z 0-9 _ + = -` with two capitals, two small
letters and two digits, and runs of 32 or more mixed hex digits); and
e-mail addresses become `<email>`. The same secret patterns then run over
every other string the digest writes, in the `.md`, the `.json` and
`seen.json` (file and function names in frames, loggers, apps, hosts,
labels), and a raw path that would need one gets no `raw_event`. Inmate names, TDCJ numbers, unit
addresses, quoted text and package codes are kept: they are largely public
and help find a cause (jstarr, 2026-11-28). Fingerprints still replace
codes, quoted text, numbers and what follows `address`, `name` or `inmate`,
so one error with different inmates stays one group. Text
is one line, at most 300 characters. Of `.env` only the `IBP_TAG=` line is
read.

**Who can read it.** The digests are `root:adm`, mode `0640`, in a
`root:adm 0750` folder: `jstarr` and `emilliken` (both in `adm`) can copy
them, e.g. `scp jstarr@ibp-server:/srv/ibp-logs/digest/2026-W48.md .`,
which is what `/ibp-errors` does. The laptops' Samba accounts can't (their shares are
their own folders). `seen.json` is root-only. If the group is missing the
digests are root-only and the report says so.

Exit status: 0 the digest was written, 1 it could not be or a source could
not be read (a file, a folder, the journal; the digest is still written,
marked incomplete), 2 bad arguments, 75 another run holds the lock
(`/var/lib/ibp-error-digest/run.lock`; counted as success by the service).

## Reading one raw event

    sudo /usr/local/bin/ibp-error-digest --show-raw '/srv/ibp-logs/IBP_LAPTOP/errors-shippy.jsonl:12'

prints that one line's fields as JSON, all but `data` (`ts`, `level`,
`app`, `version`, `host`, `logger`, `msg`, `exc`; for a kiosk journal line
`__REALTIME_TIMESTAMP`, `PRIORITY`, `SYSLOG_IDENTIFIER`, the unit fields,
`_HOSTNAME`, `MESSAGE`, ...), and reads or writes nothing else. It takes
only a `raw_event` path: below `/srv/ibp-logs` or `/srv/ibp/logs` (or
`--logs-root`/`--server-logs`), written in normal form, with each component
plain (`A-Z a-z 0-9 . _ @ + -`, not starting with a dot, so no `..`) and a
line number from 1. Each component is opened relative to the one before
without following a symlink, and the file must be a regular one, so a
file replaced by a symlink after the digest ran is refused. Exit 0
printed, 1 no such file or line (rotation shifts lines: try the `.1`
file), 2 a path it doesn't take.

Monitoring: pings a Healthchecks check (start, then the exit status with a
short summary: the first line, counts, stale stations, problems; never a
message) if `/etc/ibp-error-digest/ping-url` (root 0600) exists. Without it
there is no ping.

## Install

Run on ibp-server from a checkout of the release, in this folder:

    sudo install -D -m755 ibp-error-digest /usr/local/bin/ibp-error-digest
    sudo install -d -m755 /etc/ibp-error-digest
    sudo install -d -o root -g adm -m750 /srv/ibp-logs/digest
    sudo cp ibp-error-digest.service ibp-error-digest.timer /etc/systemd/system/
    sudo systemctl daemon-reload

Optional, in `/etc/ibp-error-digest/` (root, 0644 unless said):

- `hosts`: `<address> <name>` per line, to name the browsers' addresses in
  `client-errors.jsonl` (the addresses are in the meta repo's `CLAUDE.md`
  hosts table, LAN and tailnet).
- `muted`: `<fingerprint> <reason>` per line.
- `ping-url` (0600): the Healthchecks check's ping URL,
  `(umask 077; echo https://... | sudo tee /etc/ibp-error-digest/ping-url >/dev/null)`.

Look at a dry run (prints the digest of the last full week, writes and
pings nothing), then
enable the timer and run it once:

    sudo /usr/local/bin/ibp-error-digest --dry-run | less
    sudo systemctl enable --now ibp-error-digest.timer
    sudo systemctl start ibp-error-digest.service && journalctl -u ibp-error-digest -n 20
    ls -l /srv/ibp-logs/digest/

To try it on another machine: `--logs-root`, `--server-logs`, `--state-dir`,
`--config-dir`, `--env-file`, `--group ''`, `--no-journal`, `--offline` and
`--now` point it elsewhere (`--help`). The tests are
`tests/test_error_digest.py`.
