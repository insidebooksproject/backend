# Docker deployment on ibp-server (Raspberry Pi 4B)

Production runs as two containers on a Raspberry Pi 4B (arm64, Raspberry Pi
OS Lite 64-bit, booting from a USB SSD):

| Service | Image | What it does |
|---|---|---|
| `ibp-frontend` | `ghcr.io/insidebooksproject/frontend` | nginx on port 80: serves the built React app and proxies `/api/` to the backend |
| `ibp-backend` | `ghcr.io/insidebooksproject/backend` | FastAPI (uvicorn) on port 8000, only reachable from the frontend container |

GitHub Actions builds both images for amd64 and arm64 on every push to
`main` and on `v*` tags (`.github/workflows/docker.yml` in each repo), so
nothing is built on the Pi. They are public, tagged `main`, `sha-<sha>` and
`vX.Y.Z`. Production pins a release tag in `.env`: one `IBP_TAG` selects
both images, so both repos need the tag. Releases up to v1.0.19 exist only
under the old names, `ghcr.io/jonkensta/ibp-backend` and `ibp-frontend` (see
Upgrading).

Access is the LAN and the tailnet only. There is no authentication and no
HTTPS.

## Layout on the Pi

| Path | What |
|---|---|
| `/srv/ibp/compose.yaml`, `nginx.conf` | copied from this directory |
| `/srv/ibp/.env` | `IBP_TAG=v…` and the ISBN lookup keys (from `.env.example`; mode 0600) |
| `/srv/ibp/.env.pre-<tag>`, `compose.yaml.pre-<tag>` | the files as they were before an upgrade replaced them, kept for a rollback |
| `/srv/ibp/data/data.db` | the database, owned by `jstarr` (uid 1000), in WAL mode |
| `/srv/ibp/logs/` | the error logs, owned by the container's user (`IBP_UID`:`IBP_GID`, default 1000:1000, `jstarr`) like `data/`, mode 0750, each file 0640 (they can hold inmate data): `backend.jsonl` (the backend's own WARNING and above) and `client-errors.jsonl` (the browser reports, `POST /api/client-errors`), each rotated at 5 MiB with 4 old files kept. Not under `data/`, so the backups stay the database only |
| `/srv/ibp/data/data.db-wal`, `data.db-shm` | the database's write-ahead log and its index, there while anything has it open: part of the database, so `data.db` is never copied, moved or replaced without them while they exist (see below) |
| `~/.local/bin/ibp-backup` + timer | backups, see [`scripts/backup/`](../../scripts/backup/README.md) |
| `~/backups/ibp-pinned/` | the backup each upgrade took just before switching (`before-<tag>-<time>.db.zst`, named in `/srv/ibp/.backup-before-<tag>`), for its rollback; outside `ibp-backup`'s rotation, so never pruned: delete old ones by hand |
| `~/.local/bin/ibp-label-refund` + timer | unused-label refunds, see [`scripts/label-refund/`](../../scripts/label-refund/README.md) |
| `~/.local/bin/ibp-shipment-sync` + timer | shipment tracking, see [`scripts/shipment-sync/`](../../scripts/shipment-sync/README.md) |
| `/usr/local/bin/ibp-error-digest` + timer (root) | weekly redacted error digest in `/srv/ibp-logs/digest/`, see [`scripts/error-digest/`](../../scripts/error-digest/README.md) |

Logs: `docker compose logs -f` (rotated by Docker: 3 × 10 MB per container).
The backend also writes its WARNING and above, and the frontend's and the
Book Portal extension's error reports, to `/srv/ibp/logs/` as JSON lines in
the shared event format (see Error logs in the [top-level
README](../../README.md#error-logs)). If that directory is missing or not
writable by the container's user, the backend still starts, says so on
stderr, and writes the browser reports to stderr instead.

The backend and a sync's one-off container write the same file. Every
connection either makes (the bootstrap and its migrations too) uses WAL
and a 10 s busy timeout (`ibp/db.py`): readers and the writer don't block
each other, and a second writer waits instead of failing with "database is
locked". WAL is recorded in `data.db`, so the backend's first start on a
release that sets it switches the database over, and an older release
rolled back onto it keeps using WAL. Writes sit in
`data.db-wal` until SQLite copies them into `data.db`; the last connection
to close does that and removes both side files, but a container killed
mid-run leaves them, holding writes `data.db` lacks. So `ibp-backup` copies
the database with `sqlite3 .backup`, which reads the WAL too, and the
restore and rollback blocks move all the files together with the backend
stopped. The containers and host-side `sqlite3` share the memory-mapped
`data.db-shm` because they run on one host with `data/` on its local disk;
WAL doesn't work over a network filesystem, so `data/` must never be on one
(no Samba share exposes it).

## Setup

1. Raspberry Pi OS Lite 64-bit on the SSD (Raspberry Pi Imager: hostname,
   user `jstarr`, SSH key). Update the bootloader for USB boot first if the
   Pi doesn't boot from it. Use a blue USB 3 port; `lsusb` should show the
   ASMedia adapter and `dmesg | grep -i uas` should show no errors.

2. Packages, Docker and Tailscale:

       (
         set -Eeuo pipefail
         trap 'echo "SETUP STOPPED: fix the failed step and run this block again" >&2' ERR
         sudo apt update
         sudo apt full-upgrade -y
         sudo apt install -y sqlite3 zstd rclone curl git
         curl -fsSL https://get.docker.com | sudo sh
         sudo usermod -aG docker jstarr        # log out and back in after
         curl -fsSL https://tailscale.com/install.sh | sh
         sudo tailscale up
       )

3. The stack:

       (
         set -Eeuo pipefail
         trap 'echo "SETUP STOPPED: fix the failed step and run this block again" >&2' ERR
         sudo install -d -o jstarr -g jstarr /srv/ibp /srv/ibp/data
         sudo install -d -o jstarr -g jstarr -m 0750 /srv/ibp/logs
         # A re-run keeps what an earlier attempt made: the checkout, the
         # edited .env and the database (which the backend may have written).
         [ -d ~/ibp-backend ] || git clone https://github.com/insidebooksproject/backend.git ~/ibp-backend
         cp ~/ibp-backend/deploy/pi/{compose.yaml,nginx.conf} /srv/ibp/
         [ -e /srv/ibp/.env ] || cp ~/ibp-backend/deploy/pi/.env.example /srv/ibp/.env
         "${EDITOR:-nano}" /srv/ibp/.env     # IBP_TAG, ISBN lookup keys
         chmod 600 /srv/ibp/.env
         [ -e /srv/ibp/data/data.db ] || cp /path/to/migrated.db /srv/ibp/data/data.db
         cd /srv/ibp
         docker compose pull
         docker compose up -d
         docker compose ps                      # both "healthy"
         curl -fs localhost/api/health          # {"status":"ok","database":"connected"}
       )

   The backend runs `ibp.db_bootstrap` on every start: on an empty `data/`
   it creates a fresh schema; on a migrated database it only applies pending
   alembic migrations.

4. Backups, refunds and the shipment sync: install each from its README
   under `scripts/` (table above).

## Configuration

The backend needs no config file: the image's `sample.conf` defaults are the
production values (INFO logging to stderr, 5 s provider timeout, 60/90 day
warnings, 24 h cache), and the database path comes from `DATABASE_URL` in
`compose.yaml`. The return address and the unit mailroom line live in the
database and are edited on the app's Settings page. To change any
`sample.conf` value, mount a `server.conf` holding just the changed options
and point `CONF` at it (see Configuration in the [top-level
README](../../README.md#configuration)):

    # compose.yaml, ibp-backend:
    environment:
      CONF: /config/server.conf
    volumes:
      - ./server.conf:/config/server.conf:ro

## Upgrading

If the release changes `deploy/pi/compose.yaml` or `nginx.conf`, just
before the block keep the Pi's copy as `compose.yaml.pre-<new tag>` (or
`nginx.conf.pre-<new tag>`) with `cp -p`, `diff` it with the new tag's
file, and copy that over. The first release after v1.0.19 is one: its
`compose.yaml` names the `ghcr.io/insidebooksproject` images, while the
Pi's still names the old `ghcr.io/jonkensta` ones, which get no new tags.

Since v1.0.22 the stack mounts `./logs` into the backend, so the block
creates `/srv/ibp/logs` before it starts the new version, owned by the
container's user (`IBP_UID`, uid 1000, `jstarr`) as `data/` is: if it were
missing, Docker would create it owned by root and the backend couldn't
write it. That step changes nothing on a Pi that already has it.

Since v1.0.23 the backend makes its log files 0640 (and a log directory it
creates 0750), and the block closes what an older release left open: it
sets `/srv/ibp/logs` to 0750 and each `*.jsonl*` file in it, rotated copies
too, to 0640, all owned by the user `compose.yaml` will run the new
container as, so the backend can still write them: `IBP_UID`:`IBP_GID`
exported in the shell's environment (what `docker compose` sees), else
from the edited `.env` (quotes, spaces and a trailing comment are
ignored), else 1000. They must be plain numbers there (no `${...}`); the
block stops, saying so, before it changes anything if one isn't. Only
owners and modes change, never a file's content, and an unchanged owner is
left as it is, so it is safe to run again.

The order keeps the running version whole until a backup exists, and no
sync can start on either image until the new version is up:

1. The new `.env` is edited as a copy, `.env.next-<new tag>`, and checked
   while nothing else has changed: its `IBP_TAG` must be the new tag (and
   an `IBP_TAG` exported in the shell, which compose would use instead,
   must match it), the ids numbers.
2. The shipment timer is disabled and stopped (`disable --now`), so not
   even a reboot starts a sync until the end; whether it was enabled and
   running is kept, once. The block waits for a running sync and holds the
   sync's run lock.
3. A fresh backup (`ibp-backup`, which reads only the database, not
   `.env`), stopping if it fails. That backup is copied, never moved, to
   `~/backups/ibp-pinned/before-<new tag>-<time>.db.zst`, outside the
   backups `ibp-backup` prunes, and its path written to
   `/srv/ibp/.backup-before-<new tag>` (each attempt that gets this far
   writes its own) and printed, for a rollback.
4. Only then the log owner is set and the copy swapped in as `.env`.
5. Pull, start and wait until both containers are healthy
   (`up -d --wait`), check `/api/health`, and only then enable and start
   the timer again as it was.

If the block stops, its message says what it left and how to resume the
timer for the version `.env` names. Before the swap, `.env` names the
running version, which a reboot starts; the message says how to put the
log owner back if the ownership step or the swap failed. After the swap
(a pull or start that failed or was interrupted), finish by running the
block again: it sees that `.env` already names the new tag and only runs
the pull onward (pull, start, health check, timer), without a new backup,
so `.backup-before-<new tag>` still names the one taken before the swap.
Don't start the timer by hand before the new version is healthy, or a
sync could run the new image's migration against a stack that isn't up.
If the new version can't be made to run, roll back (below) with the
pinned backup. A re-run keeps the first saved copy of the previous `.env`
and the timer's first recorded state.

    (
      set -Eeuo pipefail
      left="nothing has changed: the shipment timer, .env and the log owner are as they were"
      trap 'echo "UPGRADE STOPPED: $left. Fix the failed step and run this block again." >&2' ERR
      cd /srv/ibp
      # Save the previous .env once: a re-run after .env was edited must
      # not replace the saved copy with the new tag.
      saved=.env.pre-<new tag>
      if [ ! -e "$saved" ]; then cp -p .env "$saved"; fi
      saved_tag=$(sed -n 's/^IBP_TAG=//p' "$saved")
      if [ -z "$saved_tag" ] || [ "$saved_tag" = "<new tag>" ]; then
        echo "NOT UPGRADED: $saved names ${saved_tag:-no IBP_TAG}, not the version to roll back to; fix it by hand first"
        exit 1
      fi
      next=.env.next-<new tag>
      timer_was=.shipment-timer-before-<new tag>
      live_tag=$(sed -n 's/^IBP_TAG=//p' .env | tail -n 1)
      if [ "$live_tag" = "<new tag>" ] && [ ! -e "$next" ]; then
        # The swap was made by an earlier run: only finish.
        echo "Finishing: .env already names <new tag>"
        left=".env names <new tag> and the shipment timer is disabled; run this block again to finish, or roll back with the backup in .backup-before-<new tag>; don't start the timer before the new version is healthy"
      else
        # The new .env is edited as a copy, kept if this block is run again:
        # .env itself changes only once a backup exists and no sync can start.
        if [ ! -e "$next" ]; then cp -p .env "$next"; fi
        "${EDITOR:-nano}" "$next"                # new IBP_TAG
        # A setting of the copy as compose reads it: the last one, without
        # quotes, spaces or a trailing comment.
        setting() {
          local value
          value=$(sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=//p" "$next" | tail -n 1)
          value=${value%%[[:space:]]#*}
          printf '%s' "$value" | tr -d "\"' \t\r"
        }
        next_tag=$(setting IBP_TAG)
        if [ "$next_tag" != "<new tag>" ]; then
          echo "NOT UPGRADED: $next names IBP_TAG '${next_tag}', not <new tag>; fix it and run this block again; nothing has changed"
          exit 1
        fi
        # Compose takes an exported IBP_TAG over .env's.
        if exported_tag=$(printenv IBP_TAG) && [ "$exported_tag" != "<new tag>" ]; then
          echo "NOT UPGRADED: IBP_TAG is exported as '$exported_tag', which compose would deploy instead of <new tag>; unset it and run this block again; nothing has changed"
          exit 1
        fi
        # The container's user, as compose will run it: IBP_UID/IBP_GID as
        # exported (what docker compose sees), else the copy's, else 1000.
        owner_id() {
          local value
          if value=$(printenv "$1"); then :; else value=$(setting "$1"); fi
          value=$(printf '%s' "$value" | tr -d "\"' \t\r")
          case "$value" in
            '') value=1000 ;;
            *[!0-9]*)
              echo "NOT UPGRADED: $1 is '$value', not a number; set it to the uid or gid itself in $next (or the environment) and run this block again; nothing has changed" >&2
              return 1 ;;
          esac
          printf '%s\n' "$value"
        }
        log_uid=$(owner_id IBP_UID)
        log_gid=$(owner_id IBP_GID)
        # No sync may start, not even after a reboot, until the new version
        # is up: one on either image could migrate the database before the
        # backup, or past the running web image. Whether the timer was
        # enabled and running is kept, once, to put it back at the end.
        if [ ! -e "$timer_was" ]; then
          enabled=$(systemctl show -p UnitFileState --value ibp-shipment-sync.timer)
          active=$(systemctl show -p ActiveState --value ibp-shipment-sync.timer)
          printf '%s %s\n' "$enabled" "$active" > "$timer_was"
        fi
      fi
      # A finishing run after the timer was already put back has no record.
      was_enabled=restored was_active=restored
      if [ -e "$timer_was" ]; then read -r was_enabled was_active < "$timer_was"; fi
      resume=""
      if [ "$was_enabled" = enabled ]; then resume="sudo systemctl enable ibp-shipment-sync.timer"; fi
      if [ "$was_active" = active ]; then resume="${resume:+$resume && }sudo systemctl start ibp-shipment-sync.timer"; fi
      resume=${resume:-nothing (it was disabled and stopped before, or is back already)}
      if [ "$live_tag" != "<new tag>" ] || [ -e "$next" ]; then
        left="the shipment timer may be disabled; .env (still $live_tag) and the log owner are unchanged, so the current version still runs; to stay on it, resume the timer: $resume"
        sudo systemctl disable --now ibp-shipment-sync.timer
        left="the shipment timer is disabled; .env (still $live_tag) and the log owner are unchanged, so the current version still runs; to stay on it, resume the timer: $resume"
        # Wait for a sync already running: the service (a running oneshot is
        # "activating") and any one-off backend container (an older wrapper's has
        # no label of its own). Each query must succeed: a failed one stops the
        # block instead of reading as "nothing running".
        while :; do
          state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
          case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
          running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
          if [ -z "$running" ]; then break; fi
          sleep 5
        done
      fi
      # Hold the sync's run lock (the wrapper's) for the rest of the block, so
      # no sync can start until it is done.
      mkdir -p ~/.local/state/ibp-shipment-sync
      exec 9>>~/.local/state/ibp-shipment-sync/run.lock
      flock -n 9 || { echo "NOT UPGRADED: a shipment sync holds its run lock; wait for it to finish and run this block again; $left"; exit 1; }
      if [ "$live_tag" != "<new tag>" ] || [ -e "$next" ]; then
        # A fresh backup of the database as the current version left it; the
        # service fails if the backup does (an integrity check included). It
        # is pinned outside ibp-backup's rotation for a rollback.
        sudo systemctl start ibp-backup.service
        backup=$(ls -1 ~/backups/ibp/data-*.db.zst | sort | tail -n 1)
        mkdir -p ~/backups/ibp-pinned
        pinned=~/backups/ibp-pinned/before-<new tag>-$(date +%Y-%m-%d_%H%M%S).db.zst
        cp -p "$backup" "$pinned"
        printf '%s\n' "$pinned" > .backup-before-<new tag>
        echo "Backup for a rollback: $pinned (named in /srv/ibp/.backup-before-<new tag>)"
        # The error logs: the new container's user owns the directory, closed
        # to other users, and its files, 0640 (no change if the owner is the
        # same). Then the new .env takes effect.
        old_owner=$(stat -c %u:%g /srv/ibp/logs 2>/dev/null || echo none)
        restore="sudo chown -R $old_owner /srv/ibp/logs"
        if [ "$old_owner" = none ]; then restore="nothing (there was no /srv/ibp/logs)"; fi
        left="the shipment timer is disabled and .env unchanged (still $live_tag), but the logs may be owned by $log_uid:$log_gid for the new version; to stay on the current version, put the owner back ($restore) and resume the timer: $resume"
        sudo install -d -o "$log_uid" -g "$log_gid" -m 0750 /srv/ibp/logs
        sudo find /srv/ibp/logs -maxdepth 1 -type f -name '*.jsonl*' -exec chown "$log_uid:$log_gid" {} + -exec chmod 0640 {} +
        mv "$next" .env
        left=".env names <new tag> and the shipment timer is disabled; run this block again to finish, or roll back with the backup in .backup-before-<new tag>; don't start the timer before the new version is healthy"
      fi
      docker compose pull
      docker compose up -d --wait
      curl -fsS -o /dev/null http://localhost/api/health
      left="<new tag> is up and healthy, but the shipment timer may not be back; resume it: $resume"
      if [ "$was_enabled" = enabled ]; then sudo systemctl enable ibp-shipment-sync.timer; fi
      if [ "$was_active" = active ]; then sudo systemctl start ibp-shipment-sync.timer; fi
      rm -f "$timer_was"
      left="<new tag> is up and healthy and the shipment timer as it was; only the clean-up of old images failed"
      docker image prune -f
    )

The links between requests and labels (`package_shipments`) are stored, and
the delivery chart reads them as stored. After an upgrade to a release that
changes only the link rules (`ibp/shipment_links.py`), rebuild them before
trusting the chart, rather than waiting for the next nightly sync:

    ~/.local/bin/ibp-shipment-sync --rebuild-links || { echo "LINKS NOT REBUILT: the chart still shows the old links; run it again"; false; }

A release that changes unit matching (`ibp/unit_places.py`,
`ibp/unit_matching.py`, `ibp/label_reading.py`) needs every stored label
matched again instead, which also rebuilds the links. Do it in this order
around the upgrade block above (it is step 5): stopping the timer doesn't
stop a run in progress, and an older wrapper takes no lock, so the new one
is installed before the backfill. A run that fails part-way says the
re-evaluation is incomplete and leaves the links as they were (see
[`scripts/shipment-sync/`](../../scripts/shipment-sync/README.md#re-evaluating-every-labels-unit)).

    (
      set -Eeuo pipefail
      trap 'echo "STOPPED before the upgrade: fix the failed step and run this block again; the timer stays stopped" >&2' ERR
      # 1. Stop the timer, so no new daily run starts.
      sudo systemctl stop ibp-shipment-sync.timer
      # 2.-3. Then:
      # Wait for a sync already running: the service (a running oneshot is
      # "activating") and any one-off backend container (an older wrapper's has
      # no label of its own). Each query must succeed: a failed one stops the
      # block instead of reading as "nothing running".
      while :; do
        state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ -z "$running" ]; then break; fi
        sleep 5
      done
      # 4. Install the new release's wrapper and units (with its run lock)
      #    before anything else runs the sync; from a checkout of the new tag:
      cd <checkout of the new tag>/scripts/shipment-sync
      install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync
      sudo cp ibp-shipment-sync.service ibp-shipment-sync.timer /etc/systemd/system/
      sudo systemctl daemon-reload
    )

Then run the upgrade block above (step 5), and only then:

    (
      set -Eeuo pipefail
      trap 'echo "STOPPED: the step above failed; the timer stays stopped" >&2' ERR
      # 6. Re-evaluate every label from the first label's date until it exits
      #    0; it is re-run only after an incomplete run (1); any other status
      #    (2 no key, 75 another run) stops the loop to be looked at.
      while true; do status=0; ~/.local/bin/ibp-shipment-sync --since 2025-11-01 || status=$?; [ "$status" -eq 1 ] || break; echo "not complete; again in 60 s"; sleep 60; done
      # 7. Start the timer again, only if the backfill completed (exit 0).
      if [ "$status" = 0 ]; then
        sudo systemctl start ibp-shipment-sync.timer
      else
        echo "TIMER STILL STOPPED: the backfill exited $status; run this block again until it exits 0; do not start the timer before"
        exit 1
      fi
    )

The sync's run lock, which the upgrade, rollback and [restore](../../scripts/backup/README.md#restore)
blocks hold, is described in [`scripts/shipment-sync/`](../../scripts/shipment-sync/README.md).

## Rolling back

Roll back by setting the previous tag and running `docker compose up -d`
again. If the upgrade replaced `compose.yaml` or `nginx.conf`, first put
the saved `.pre-<new tag>` copy back (`cp -p`); a rollback to v1.0.19 or
earlier always needs the old `compose.yaml`, since those tags exist only
under the old image names.

If the new release ran a database migration, the old backend can't start
on the migrated database, so restore the backup taken just before the
upgrade with the block below, `<pinned backup>` in it replaced by the
path in `/srv/ibp/.backup-before-<new tag>` (the upgrade printed it; it is
pinned in `~/backups/ibp-pinned/`, outside the backups `ibp-backup`
prunes). Writes since then are lost, but the database it replaces is
kept, with its SQLite side files, which must not stay beside the restored
database, in a new `data/before-rollback-…` directory. It stops the shipment timer and holds
the sync's run lock throughout, so no sync can migrate the restored
database, and it stops the backup timer and waits for a backup in progress,
so none has the database open while it is swapped; it checks everything
that can fail before moving any file, and it can be run again after a
failure. It leaves the shipment timer stopped, and starts the backup timer
again at the end if it was running:

    (
      set -Eeuo pipefail
      trap 'echo "ROLLBACK STOPPED: the step above failed; the shipment and backup timers stay stopped" >&2' ERR
      cd /srv/ibp
      # Every check that can fail comes before anything is moved.
      previous=.env.pre-<new tag>
      [ -s "$previous" ] || { echo "NOT ROLLED BACK: $previous is missing or empty; nothing was changed"; exit 1; }
      previous_tag=$(sed -n 's/^IBP_TAG=//p' "$previous")
      [ -n "$previous_tag" ] || { echo "NOT ROLLED BACK: $previous names no IBP_TAG; nothing was changed"; exit 1; }
      # No sync may start on the new image and migrate the restored database,
      # and no backup may have the database open while it is swapped. The
      # backup timer's state is kept, once, to restore it at the end.
      backup_was=.backup-timer-before-rollback
      if [ ! -e "$backup_was" ]; then
        timer_state=$(systemctl show -p ActiveState --value ibp-backup.timer)
        printf '%s\n' "$timer_state" > "$backup_was"
      fi
      sudo systemctl stop ibp-shipment-sync.timer ibp-backup.timer
      # Wait for a sync or a backup already running: the services (a running
      # oneshot is "activating") and any one-off backend container (an older
      # wrapper's has no label of its own). Each query must succeed: a failed
      # one stops the block instead of reading as "nothing running".
      while :; do
        state=$(systemctl show -p ActiveState --value ibp-shipment-sync.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        state=$(systemctl show -p ActiveState --value ibp-backup.service)
        case "$state" in active | activating | deactivating | reloading) sleep 5; continue ;; esac
        running=$(docker ps -q --filter label=com.docker.compose.oneoff=True --filter label=com.docker.compose.service=ibp-backend)
        if [ -z "$running" ]; then break; fi
        sleep 5
      done
      # Hold the sync's run lock (the wrapper's) for the rest of the block, so
      # no sync can start until it is done.
      mkdir -p ~/.local/state/ibp-shipment-sync
      exec 9>>~/.local/state/ibp-shipment-sync/run.lock
      flock -n 9 || { echo "NOT ROLLED BACK: a shipment sync holds its run lock; wait for it to finish and run this block again"; exit 1; }
      # The containers must be stopped.
      docker compose down || { echo "NOT RESTORED: the containers did not stop; nothing was changed"; exit 1; }
      # A fresh staging file, so no older one can be taken for this backup.
      mkdir -p ~/restore
      staging=~/restore/data-$EPOCHSECONDS-$$.db
      rm -f "$staging"
      zstd -d <pinned backup> -o "$staging" || { echo "NOT RESTORED: the backup did not decompress; nothing was changed"; exit 1; }
      check=$(sqlite3 "$staging" 'PRAGMA integrity_check;') || check="failed"
      [ "$check" = ok ] || { echo "NOT RESTORED: the backup failed its integrity check; nothing was changed; pick another backup"; exit 1; }
      # Stage the new files beside the live ones, and a new directory for
      # the live database (never one that holds a database a re-run could
      # overwrite). An attempt stopped part-way through the swap, after it
      # moved data.db aside, left its directory's name in the marker: its
      # remaining SQLite files (-wal, -shm) go to that same directory, so
      # the kept database stays whole.
      install -m 0644 "$staging" data/data.db.restoring
      cp "$previous" .env.restoring
      marker=data/.rollback-in-progress
      aside=""
      if [ -s "$marker" ] && [ ! -e data/data.db ]; then
        read -r aside < "$marker"
        if [ ! -e "$aside/data.db" ]; then aside=""; fi
      fi
      if [ -z "$aside" ]; then
        aside=data/before-rollback-$EPOCHSECONDS-$$
        mkdir "$aside"
        cp -p .env "$aside/.env"
        printf '%s\n' "$aside" > "$marker"
      fi
      # The swap: renames only, never onto a file already kept.
      for file in "" -journal -wal -shm; do
        if [ -e "data/data.db$file" ]; then
          [ ! -e "$aside/data.db$file" ] || { echo "NOT ROLLED BACK: $aside/data.db$file already exists; nothing more was moved"; exit 1; }
          mv "data/data.db$file" "$aside/data.db$file"
        fi
      done
      mv data/data.db.restoring data/data.db
      mv .env.restoring .env
      rm -f "$marker"
      docker compose up -d
      docker compose ps                         # both on the old tag, healthy
      read -r timer_state < "$backup_was"
      if [ "$timer_state" = active ]; then sudo systemctl start ibp-backup.timer; fi
      rm -f "$backup_was"
      echo "ROLLED BACK to $previous_tag; the database it replaced is in /srv/ibp/$aside. The shipment timer is still stopped: start it (sudo systemctl start ibp-shipment-sync.timer) only once the old version is confirmed healthy, and first reinstall the old wrapper and units from a checkout of $previous_tag if the rollback crosses the release that changed them."
    )
