"""The runbooks' shell blocks (deploy/pi/README.md, scripts/shipment-sync/
README.md), run in a sandbox with stand-in commands.

Each block must stop at its first failed step, an action or a status query,
with a non-zero status; run again after such a failure it must end as a
clean run does, without overwriting anything an earlier attempt kept.
"""

# pylint: disable=redefined-outer-name

import re
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

TOPLEVEL = Path(__file__).resolve().parent.parent
RUNBOOKS = ["deploy/pi/README.md", "scripts/shipment-sync/README.md"]
LOCK = ".local/state/ibp-shipment-sync/run.lock"

# Stand-ins. Every action and every status query is a step: it is logged,
# fails if it is step $FAIL_AT, and otherwise does what the real command
# would to the sandbox (files really move). The shell an installer is
# piped into is not a step (pipefail fails the pipeline on the download);
# sudo runs its command. With $PROBE set, a stand-in sync tries the run
# lock when a step starting with it runs, and logs whether it got it.
PRELUDE = r"""
step() {
  local n; read -r n < "$COUNT"; n=$((n + 1)); echo "$n" > "$COUNT"
  printf '%s\n' "$*" >> "$LOG"
  if [ -n "${PROBE:-}" ] && [[ "$*" == "$PROBE"* ]]; then
    if command flock -n "$HOME/.local/state/ibp-shipment-sync/run.lock" true; then
      echo acquired >> "$PROBE_LOG"; else echo refused >> "$PROBE_LOG"; fi
  fi
  [ "$n" -ne "$FAIL_AT" ]
}
for command in cp mv rm mkdir chmod flock; do
  eval "$command() { step $command \"\$@\" && command $command \"\$@\"; }"
done
for command in apt curl journalctl tailscale usermod; do
  eval "$command() { step $command \"\$@\"; }"
done
cd() { step cd "$@" && builtin cd "$@"; }
sh() { :; }
sleep() { :; }
sudo() { "$@"; }
install() {
  step install "$@" || return 1
  local args=() skip=0
  for arg in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$arg" in -o | -g) skip=1 ;; *) args+=("$arg") ;; esac
  done
  command install "${args[@]}"
}
cat() { step cat "$@" && printf 'EZAKtest\n'; }
nano() { step nano "$@" && printf 'IBP_TAG=X\n%s' "${NANO_EXTRA:-}" > "$1"; }
git() {
  step git "$@" || return 1
  command mkdir -p "$3/deploy/pi"
  printf 'services: {}\n' > "$3/deploy/pi/compose.yaml"
  printf 'events {}\n' > "$3/deploy/pi/nginx.conf"
  printf 'IBP_TAG=example\n' > "$3/deploy/pi/.env.example"
}
zstd() { step zstd "$@" && command cp "$2" "$4"; }
sqlite3() { step sqlite3 "$@" || return 1; printf '%s\n' "${SQLITE_OUT-ok}"; }
docker() {
  step docker "$@" || return 1
  if [ "$1" = ps ]; then printf '%s' "${CONTAINERS:-}"; fi
}
systemctl() {
  step systemctl "$@" || return 1
  if [ "$*" = "start ibp-backup.service" ]; then
    printf 'backup\n' > "$HOME/backups/ibp/data-upgrade.db.zst"
  fi
  if [ "$1" = show ]; then
    case "$*" in
      *UnitFileState*) echo "${TIMER_ENABLED:-enabled}" ;;
      *.timer) echo "${TIMER_STATE:-inactive}" ;;
      *) echo "${STATE:-inactive}" ;;
    esac
  fi
}
ibp_sync() {
  if [ -n "${ANSWERS:-}" ]; then
    local status; read -r status < "$ANSWERS"; command sed -i 1d "$ANSWERS"
    step ibp-shipment-sync "$@"; return "$status"
  fi
  step ibp-shipment-sync "$@" || return "${SYNC_FAIL:-2}"
}
"""


def code_blocks(readme: str) -> list[str]:
    """The indented code blocks of a Markdown file (4 spaces, or 7 in a
    numbered list), dedented by 4."""
    blocks, current = [], []
    for line in (TOPLEVEL / readme).read_text().splitlines():
        if line.startswith("    "):
            current.append(line[4:])
        elif line.strip() and current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def is_shell(block: str) -> bool:
    """Every block is shell but the illustrative compose.yaml snippet."""
    return "environment:" not in block


SHELL_BLOCKS = [
    pytest.param(readme, index, id=f"{readme}#{index}")
    for readme in RUNBOOKS
    for index, block in enumerate(code_blocks(readme))
    if is_shell(block)
]


class Result(NamedTuple):
    """How a block ran: its status, the steps it ran, its output."""

    status: int
    steps: list[str]
    output: str


def sandbox(tmp_path: Path) -> Path:
    """A fresh sandbox: a deployed stack, a backup, a release checkout."""
    root = tmp_path / "sandbox"
    srv = root / "srv" / "ibp"
    (srv / "data").mkdir(parents=True)
    (srv / ".env").write_text("IBP_TAG=v1.0.0\n")
    (srv / ".env.pre-X").write_text("IBP_TAG=v0.9.0\n")
    (srv / "data" / "data.db").write_text("live database\n")
    (srv / "data" / "data.db-wal").write_text("live wal\n")
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    (root / "migrated.db").write_text("migrated database\n")
    checkout = root / "checkout" / "scripts" / "shipment-sync"
    checkout.mkdir(parents=True)
    for name in (
        "ibp-shipment-sync",
        "ibp-shipment-sync.service",
        "ibp-shipment-sync.timer",
    ):
        (checkout / name).write_text(f"new {name}\n")
    home = root / "home"
    (home / ".config" / "ibp-shipment-sync").mkdir(parents=True)
    (home / "backups" / "ibp").mkdir(parents=True)
    (home / "backups" / "ibp" / "data-X.db.zst").write_text("backup database\n")
    return root


def without_upgrade_save(root: Path) -> Path:
    """The sandbox before an upgrade: no saved .env yet."""
    (root / "srv" / "ibp" / ".env.pre-X").unlink()
    return root


def script_for(block: str, root: Path) -> str:
    """``block`` pointed at the sandbox, its placeholders filled in."""
    script = block.replace("~/.local/bin/ibp-shipment-sync --", "ibp_sync --")
    script = script.replace("<checkout of the new tag>", str(root / "checkout"))
    script = script.replace("<pinned backup>", "~/backups/ibp/data-X.db.zst")
    for placeholder in ("<new tag>", "<before upgrade>"):
        script = script.replace(placeholder, "X")
    for path in ("/srv/ibp", "/etc/systemd/system", "/path/to/migrated.db"):
        script = script.replace(path, f"{root}{path}")
    return script


def run_block(
    root: Path, block: str, fail_at: int = 0, env: Optional[dict] = None
) -> Result:
    """Run ``block`` in sandbox ``root``, failing step ``fail_at`` (0: none)."""
    count, log = root.parent / "count", root.parent / "log"
    count.write_text("0\n")
    log.write_text("")
    result = subprocess.run(
        ["bash", "-c", PRELUDE + script_for(block, root)],
        text=True,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        # Blocks without a cd of their own run from the wrapper's directory
        # of a checkout, as the install section says.
        cwd=root / "checkout" / "scripts" / "shipment-sync",
        env={
            "HOME": str(root / "home"),
            "PATH": "/usr/bin:/bin",
            "COUNT": str(count),
            "LOG": str(log),
            "FAIL_AT": str(fail_at),
            "PROBE_LOG": str(root.parent / "probe"),
            **(env or {}),
        },
    )
    # Names unique to an attempt (the rollback's staging file and kept
    # database) are compared without their unique part.
    steps = [unique(step, root) for step in log.read_text().splitlines()]
    return Result(result.returncode, steps, result.stdout + result.stderr)


def unique(text: str, root: Path) -> str:
    """``text`` with per-attempt names and the sandbox path made generic."""
    text = re.sub(r"(data|before-rollback)-\d+-\d+", r"\1-UNIQUE", text)
    text = re.sub(r"(before-[^/\s]+?)-\d{4}-\d\d-\d\d_\d{6}", r"\1-UNIQUE", text)
    return text.replace(str(root), "")


def state(root: Path) -> dict[str, str]:
    """Every file of the sandbox and its content, attempt-unique names made
    generic, the kept databases left out (checked on their own)."""
    found = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        # The pinned backups: one per attempt that got that far.
        pinned = "backups/ibp-pinned/" in relative
        if path.is_file() and "before-rollback-" not in relative and not pinned:
            found[unique(relative, root)] = unique(path.read_text(), root)
    return found


def kept(root: Path) -> dict[str, str]:
    """What runs keep for a later rollback or to look at: the saved .env and
    each moved-aside database directory."""
    files = list((root / "srv" / "ibp").glob(".env.pre-*"))
    files += list((root / "srv" / "ibp" / "data").glob("before-rollback-*/*"))
    return {str(path.relative_to(root)): path.read_text() for path in files}


def block_at(readme: str, index: int) -> str:
    """The ``index``-th code block of ``readme``."""
    return code_blocks(readme)[index]


def fresh(tmp_path: Path, block: str, name: str) -> Path:
    """A sandbox for ``block`` (an upgrade starts with no saved .env)."""
    root = sandbox(tmp_path / name)
    if "saved=.env.pre-" in block:
        without_upgrade_save(root)
    return root


@pytest.mark.parametrize("readme, index", SHELL_BLOCKS)
def test_block_is_valid_shell(readme, index):
    """Every shell block parses."""
    script = block_at(readme, index).replace("<", "_").replace(">", "_")
    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("readme, index", SHELL_BLOCKS)
def test_block_stops_at_its_first_failed_step(tmp_path, readme, index):
    """With every step succeeding the block exits 0 having run them all;
    failing any one step, a status query included, stops it there with a
    non-zero status."""
    block = block_at(readme, index)
    success = run_block(fresh(tmp_path, block, "clean"), block)
    assert success.status == 0, success.output
    assert success.steps
    for fail_at in range(1, len(success.steps) + 1):
        failed = run_block(fresh(tmp_path, block, f"fail{fail_at}"), block, fail_at)
        assert failed.status != 0, (fail_at, failed.output)
        assert failed.steps == success.steps[:fail_at], (fail_at, failed.output)


@pytest.mark.parametrize("readme, index", SHELL_BLOCKS)
def test_block_run_again_after_a_failure_ends_as_a_clean_run(tmp_path, readme, index):
    """Failing at any step and running the whole block again ends in the
    state a clean run leaves, and the second run overwrites nothing the
    first kept (the saved .env, a moved-aside database)."""
    block = block_at(readme, index)
    clean = fresh(tmp_path, block, "clean")
    steps = run_block(clean, block).steps
    for fail_at in range(1, len(steps) + 1):
        root = fresh(tmp_path, block, f"retry{fail_at}")
        run_block(root, block, fail_at)
        kept_before = kept(root)
        again = run_block(root, block)
        assert again.status == 0, (fail_at, again.output)
        assert state(root) == state(clean), fail_at
        kept_after = kept(root)
        assert {path: kept_after.get(path) for path in kept_before} == kept_before


def rollback_block() -> str:
    """The rollback block of deploy/pi/README.md."""
    (block,) = [
        b for b in code_blocks("deploy/pi/README.md") if "docker compose down" in b
    ]
    return block


def upgrade_block() -> str:
    """The upgrade block of deploy/pi/README.md."""
    (block,) = [b for b in code_blocks("deploy/pi/README.md") if "saved=.env.pre-" in b]
    return block


def pre_upgrade_block(readme: str) -> str:
    """The re-evaluation's steps 1-4 block."""
    (block,) = [b for b in code_blocks(readme) if "# 1. Stop the timer" in b]
    return block


def test_rollback_checks_everything_before_moving_anything(tmp_path):
    """The rollback stops the timer, drains and locks, stops the
    containers and verifies a fresh staging file before the first move;
    after the swap only clearing its marker, starting the containers and
    clearing the backup timer's saved state remain (the timer was not
    running, so it is not started)."""
    root = sandbox(tmp_path)
    steps = run_block(root, rollback_block()).steps
    first_move = next(n for n, step in enumerate(steps) if step.startswith("mv "))
    before = " ".join(steps[:first_move])
    for needed in (
        "systemctl stop ibp-shipment-sync.timer",
        "systemctl show -p ActiveState",
        "docker ps",
        "flock -n 9",
        "docker compose down",
        "zstd -d",
        "sqlite3",
        "install -m 0644",
        "cp .env.pre-X .env.restoring",
    ):
        assert needed in before, needed
    after = [step for step in steps[first_move:] if not step.startswith("mv ")]
    assert after == [
        "rm -f data/.rollback-in-progress",
        "docker compose up -d",
        "docker compose ps",
        "rm -f .backup-timer-before-rollback",
    ]
    srv = root / "srv" / "ibp"
    assert (srv / "data" / "data.db").read_text() == "backup database\n"
    assert (srv / ".env").read_text() == "IBP_TAG=v0.9.0\n"
    (aside,) = (srv / "data").glob("before-rollback-*")
    assert (aside / "data.db").read_text() == "live database\n"
    assert (aside / "data.db-wal").read_text() == "live wal\n"
    assert (aside / ".env").read_text() == "IBP_TAG=v1.0.0\n"


def test_rollback_run_again_keeps_the_first_kept_database(tmp_path):
    """The reported case: a failure after the swap, then a re-run. The
    first attempt's copy of the post-upgrade database is never replaced."""
    root = sandbox(tmp_path)
    steps = run_block(root, rollback_block()).steps
    root = sandbox(tmp_path / "retry")
    up = steps.index("docker compose up -d") + 1
    assert run_block(root, rollback_block(), up).status != 0
    (first,) = (root / "srv" / "ibp" / "data").glob("before-rollback-*")
    assert run_block(root, rollback_block()).status == 0
    assert (first / "data.db").read_text() == "live database\n"
    assert len(list((root / "srv" / "ibp" / "data").glob("before-rollback-*"))) == 2


def test_rollback_stopped_mid_swap_keeps_the_database_with_its_wal(tmp_path):
    """A failure after data.db was moved aside but before its -wal: the
    re-run puts the -wal beside the database it belongs to, not in a second
    directory."""
    root = sandbox(tmp_path)
    steps = run_block(root, rollback_block()).steps
    root = sandbox(tmp_path / "retry")
    wal = next(n for n, step in enumerate(steps) if "data.db-wal" in step) + 1
    assert run_block(root, rollback_block(), wal).status != 0
    data = root / "srv" / "ibp" / "data"
    (first,) = data.glob("before-rollback-*")
    assert (first / "data.db").exists() and not (first / "data.db-wal").exists()
    assert (data / "data.db-wal").read_text() == "live wal\n"

    assert run_block(root, rollback_block()).status == 0

    assert list(data.glob("before-rollback-*")) == [first]
    assert (first / "data.db").read_text() == "live database\n"
    assert (first / "data.db-wal").read_text() == "live wal\n"
    assert (first / ".env").read_text() == "IBP_TAG=v1.0.0\n"
    assert (data / "data.db").read_text() == "backup database\n"
    assert not (data / ".rollback-in-progress").exists()
    assert not (data / "data.db-wal").exists()


def test_rollback_holds_the_sync_lock_through_the_switch(tmp_path):
    """A stand-in sync trying the run lock while the database and tag are
    switched is refused; the block also fails clearly if a sync holds it."""
    root = sandbox(tmp_path)
    for probe in ("mv data/data.db", "mv .env.restoring", "docker compose up"):
        (root.parent / "probe").unlink(missing_ok=True)
        result = run_block(
            sandbox(tmp_path / probe.replace(" ", "_")),
            rollback_block(),
            env={"PROBE": probe},
        )
        assert result.status == 0
        tries = (tmp_path / probe.replace(" ", "_") / "probe").read_text().split()
        assert tries and set(tries) == {"refused"}, probe
    held = sandbox(tmp_path / "held")
    (held / "home" / LOCK).parent.mkdir(parents=True)
    with open(held / "home" / LOCK, "a", encoding="utf-8") as lock:
        subprocess.run(["flock", "-n", str(held / "home" / LOCK), "true"], check=True)
        locker = subprocess.Popen(  # pylint: disable=consider-using-with
            ["flock", str(held / "home" / LOCK), "sleep", "5"]
        )
        try:
            result = run_block(held, rollback_block())
        finally:
            locker.kill()
            locker.wait()
            lock.close()
    assert result.status != 0
    assert "a shipment sync holds its run lock" in result.output
    assert (held / "srv" / "ibp" / "data" / "data.db").read_text() == "live database\n"


def test_rollback_leaves_the_timer_stopped_and_says_when_to_start_it(tmp_path):
    """The rollback never starts the timer, and ends saying when to."""
    result = run_block(sandbox(tmp_path), rollback_block())
    assert not [
        s for s in result.steps if "systemctl start ibp-shipment-sync.timer" in s
    ]
    assert "The shipment timer is still stopped" in result.output
    assert "reinstall the old wrapper and units" in result.output


@pytest.mark.parametrize("missing", ["file", "tag"])
def test_rollback_without_a_saved_tag_changes_nothing(tmp_path, missing):
    """No saved .env, or one naming no tag, stops before anything else."""
    root = sandbox(tmp_path)
    saved = root / "srv" / "ibp" / ".env.pre-X"
    if missing == "file":
        saved.unlink()
    else:
        saved.write_text("SOMETHING=else\n")
    result = run_block(root, rollback_block())
    assert result.status != 0 and "NOT ROLLED BACK" in result.output
    assert result.steps == ["cd /srv/ibp"]


@pytest.mark.parametrize("output", ["", "not ok", "ok\nok", "*** in database main ***"])
def test_rollback_needs_exactly_ok_from_the_integrity_check(tmp_path, output):
    """Anything but exactly "ok" stops the rollback before the live database
    is touched."""
    root = sandbox(tmp_path)
    result = run_block(root, rollback_block(), env={"SQLITE_OUT": output})
    assert result.status != 0
    assert not [step for step in result.steps if step.split()[0] in {"mv", "install"}]
    assert "NOT RESTORED: the backup failed its integrity check" in result.output
    assert (root / "srv" / "ibp" / "data" / "data.db").read_text() == "live database\n"


DRAINING = [
    pytest.param(rollback_block, id="rollback"),
    pytest.param(upgrade_block, id="upgrade"),
    *(
        pytest.param(lambda readme=readme: pre_upgrade_block(readme), id=readme)
        for readme in RUNBOOKS
    ),
]


@pytest.mark.parametrize("block", DRAINING)
@pytest.mark.parametrize(
    "env", [{"STATE": "activating"}, {"STATE": "active"}, {"CONTAINERS": "abc123"}]
)
def test_drain_waits_while_a_sync_runs(tmp_path, block, env):
    """A sync still running (the service active or activating, or a one-off
    container) keeps the drain waiting: with a stand-in that never stops,
    the block never gets past it."""
    root = fresh(tmp_path, block(), "busy")
    (tmp_path / "count").write_text("0\n")
    (tmp_path / "log").write_text("")
    script = PRELUDE + script_for(block(), root)
    result = subprocess.run(
        ["timeout", "2", "bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        cwd=root,
        env={
            "HOME": str(root / "home"),
            "PATH": "/usr/bin:/bin",
            "COUNT": str(tmp_path / "count"),
            "LOG": str(tmp_path / "log"),
            "FAIL_AT": "0",
            **env,
        },
    )
    assert result.returncode == 124  # still waiting when timed out
    log = (tmp_path / "log").read_text()
    # The upgrade's .env edit is on a copy, before the drain; the copy
    # replaces .env (mv .env.next-...) only after it.
    for later in ("flock", "install -D", "compose down", "mv .env.next"):
        assert later not in log


def test_upgrade_run_again_keeps_the_first_saved_tag(tmp_path):
    """The reported case: a failed pull after .env was edited, then a
    re-run. The saved .env still names the previous tag."""
    block = upgrade_block()
    root = without_upgrade_save(sandbox(tmp_path))
    steps = run_block(root, block).steps
    root = without_upgrade_save(sandbox(tmp_path / "retry"))
    pull = steps.index("docker compose pull") + 1
    assert run_block(root, block, pull).status != 0
    assert (root / "srv" / "ibp" / ".env").read_text() == "IBP_TAG=X\n"
    assert run_block(root, block).status == 0
    assert (root / "srv" / "ibp" / ".env.pre-X").read_text() == "IBP_TAG=v1.0.0\n"


def test_upgrade_refuses_a_saved_copy_naming_the_new_tag(tmp_path):
    """A saved .env already naming the new tag is refused before anything."""
    root = without_upgrade_save(sandbox(tmp_path))
    (root / "srv" / "ibp" / ".env.pre-X").write_text("IBP_TAG=X\n")
    result = run_block(root, upgrade_block())
    assert result.status != 0 and "NOT UPGRADED" in result.output
    assert not [s for s in result.steps if "compose" in s or "systemctl stop" in s]


DISABLE = "systemctl disable --now ibp-shipment-sync.timer"
ENABLE = "systemctl enable ibp-shipment-sync.timer"
START = "systemctl start ibp-shipment-sync.timer"
HEALTH = "curl -fsS -o /dev/null http://localhost/api/health"
UPGRADE_ENV = Path("srv/ibp/.env")
MARKER = Path("srv/ibp/.backup-before-X")


def fresh_upgrade(tmp_path: Path, name: str) -> Path:
    """A sandbox before an upgrade."""
    return without_upgrade_save(sandbox(tmp_path / name))


def upgrade_steps(tmp_path: Path) -> list[str]:
    """The steps of a clean upgrade."""
    return run_block(fresh_upgrade(tmp_path, "clean"), upgrade_block()).steps


@pytest.mark.parametrize(
    "enabled, active, put_back",
    [
        ("enabled", "active", [ENABLE, START]),
        ("enabled", "inactive", [ENABLE]),
        ("disabled", "inactive", []),
    ],
)
def test_upgrade_disables_the_timer_and_puts_it_back_once_healthy(
    tmp_path, enabled, active, put_back
):
    """The timer is disabled (so not even a reboot starts a sync) until the
    new version is up, healthy and answering; then it is enabled and
    started only as it was, even when that took a re-run (its state is kept
    from the first)."""
    env = {"TIMER_ENABLED": enabled, "TIMER_STATE": active}
    root = fresh_upgrade(tmp_path, "run")
    steps = run_block(root, upgrade_block(), env=env).steps
    assert DISABLE in steps
    after = steps[steps.index(HEALTH) + 1 :]
    assert [s for s in after if s in (ENABLE, START)] == put_back
    assert steps.index("docker compose up -d --wait") < steps.index(HEALTH)
    # A re-run after a failed pull sees the timer disabled and stopped, but
    # puts back what the first run recorded.
    retry = fresh_upgrade(tmp_path, "retry")
    run_block(retry, upgrade_block(), steps.index("docker compose pull") + 1, env)
    gone = {"TIMER_ENABLED": "disabled", "TIMER_STATE": "inactive"}
    again = run_block(retry, upgrade_block(), env=gone).steps
    assert [s for s in again if s in (ENABLE, START)] == put_back
    probe = fresh_upgrade(tmp_path, "probe")
    run_block(probe, upgrade_block(), env={"PROBE": "docker compose pull"})
    assert (tmp_path / "probe" / "probe").read_text() == "refused\n"


@pytest.mark.parametrize("step", ["docker compose up -d --wait", HEALTH])
def test_upgrade_leaves_the_timer_disabled_unless_healthy(tmp_path, step):
    """If the new version doesn't come up healthy, the timer stays disabled
    and the message says not to start it."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "failed")
    result = run_block(root, upgrade_block(), clean.index(step) + 1)
    assert result.status != 0
    assert DISABLE in result.steps
    assert not [s for s in result.steps if s in (ENABLE, START)]
    assert ".env names X and the shipment timer is disabled" in result.output
    assert "don't start the timer before the new version is healthy" in result.output


@pytest.mark.parametrize(
    "edited, shell, owner",
    [
        ("", {}, "1000 -g 1000"),
        ("IBP_UID=1001\nIBP_GID=1002\n", {}, "1001 -g 1002"),
        ("IBP_UID=\"1001\"\nIBP_GID='1002'\n", {}, "1001 -g 1002"),
        ("  IBP_UID = 1001  # jstarr\r\nIBP_GID=1002\t\n", {}, "1001 -g 1002"),
        ("export IBP_UID=1001\nIBP_UID=1003\n", {}, "1003 -g 1000"),
        ("IBP_UID=\nIBP_GID=\n", {}, "1000 -g 1000"),
        # As compose does, the exported environment wins over .env.
        ("IBP_UID=1001\n", {"IBP_UID": "1005", "IBP_GID": ""}, "1005 -g 1000"),
        # An exported IBP_TAG naming the new tag is fine.
        ("", {"IBP_TAG": "X"}, "1000 -g 1000"),
    ],
)
def test_upgrade_switches_only_after_a_backup(tmp_path, edited, shell, owner):
    """The edited copy is checked before the timer is disabled; the backup
    comes after the drain and the lock, is pinned and named for the
    rollback, and only then the log owner (from IBP_UID/IBP_GID as compose
    gets them, else 1000) and the swap of the copy in as .env."""
    env = {"NANO_EXTRA": edited, **shell}
    root = fresh_upgrade(tmp_path, "run")
    result = run_block(root, upgrade_block(), env=env)
    assert result.status == 0, result.output
    install = f"install -d -o {owner} -m 0750 /srv/ibp/logs"
    steps = result.steps
    order = [
        "nano .env.next-X",
        DISABLE,
        "flock -n 9",
        "systemctl start ibp-backup.service",
        "mkdir -p /home/backups/ibp-pinned",
        install,
        "mv .env.next-X .env",
        "docker compose pull",
        "docker compose up -d --wait",
        HEALTH,
        ENABLE,
    ]
    positions = [steps.index(step) for step in order]
    assert positions == sorted(positions)
    assert (root / UPGRADE_ENV).read_bytes() == f"IBP_TAG=X\n{edited}".encode()
    assert not (root / "srv" / "ibp" / ".env.next-X").exists()
    pinned = Path((root / MARKER).read_text().strip())
    assert pinned.parent == root / "home" / "backups" / "ibp-pinned"
    assert re.fullmatch(r"before-X-\d{4}-\d\d-\d\d_\d{6}\.db\.zst", pinned.name)
    # A copy, not the rotating backup itself.
    assert pinned.read_text() == "backup\n"
    assert (root / "home" / "backups" / "ibp" / "data-upgrade.db.zst").exists()
    assert f"Backup for a rollback: {pinned}" in result.output


@pytest.mark.parametrize(
    "step", [DISABLE, "flock -n 9", "systemctl start ibp-backup.service"]
)
def test_upgrade_stopped_before_the_switch_leaves_the_running_version(tmp_path, step):
    """A failed backup (or drain, or lock) leaves .env naming the running
    version, so a reboot runs the old image, and the log owner untouched;
    the message says so and how to resume the timer."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "failed")
    env = {"NANO_EXTRA": "IBP_UID=1001\n", "TIMER_STATE": "active"}
    result = run_block(root, upgrade_block(), clean.index(step) + 1, env)
    assert result.status != 0
    assert (root / UPGRADE_ENV).read_text() == "IBP_TAG=v1.0.0\n"
    assert not [s for s in result.steps if s.startswith(("install -d", "mv .env"))]
    assert not (root / "srv" / "ibp" / "logs").exists()
    assert ".env (still v1.0.0) and the log owner are unchanged" in result.output
    assert (
        "resume the timer: sudo systemctl enable ibp-shipment-sync.timer && "
        "sudo systemctl start ibp-shipment-sync.timer" in result.output
    )


@pytest.mark.parametrize("step", ["install -d", "mv .env.next-X .env"])
def test_upgrade_stopped_at_the_owner_says_how_to_put_it_back(tmp_path, step):
    """If setting the log owner or the swap fails, .env is unchanged and the
    message gives the command to put the old owner back."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "failed")
    logs = root / "srv" / "ibp" / "logs"
    logs.mkdir()
    old = f"{logs.stat().st_uid}:{logs.stat().st_gid}"
    at = next(i for i, s in enumerate(clean) if s.startswith(step)) + 1
    result = run_block(root, upgrade_block(), at)
    assert result.status != 0
    assert (root / UPGRADE_ENV).read_text() == "IBP_TAG=v1.0.0\n"
    output = unique(result.output, root)
    assert ".env unchanged (still v1.0.0)" in output
    assert f"put the owner back (sudo chown -R {old} /srv/ibp/logs)" in output


def test_upgrade_run_again_after_the_switch_only_finishes(tmp_path):
    """Once .env names the new tag, a re-run takes no new backup (the
    marker still names the one from before the swap), doesn't edit, and
    finishes: pull, start, health, timer."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "retry")
    run_block(root, upgrade_block(), clean.index("docker compose pull") + 1)
    marker = (root / MARKER).read_text()
    again = run_block(root, upgrade_block())
    assert again.status == 0, again.output
    assert "Finishing: .env already names X" in again.output
    for step in ("nano", "systemctl start ibp-backup.service", "mv .env", "install -d"):
        assert not [s for s in again.steps if s.startswith(step)], step
    assert again.steps.index("docker compose up -d --wait") < again.steps.index(ENABLE)
    assert (root / MARKER).read_text() == marker


@pytest.mark.parametrize(
    "live",
    [
        'IBP_TAG="X"\n',
        "IBP_TAG=X  # the new release\n",
        "export IBP_TAG=X\n",
        "IBP_TAG=v1.0.0\nIBP_TAG='X'\n",
    ],
)
def test_upgrade_reads_the_live_tag_as_compose_does(tmp_path, live):
    """However .env writes the new tag after the switch, a re-run sees the
    switch and only finishes: no new backup, the marker kept."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "retry")
    run_block(root, upgrade_block(), clean.index("docker compose pull") + 1)
    (root / UPGRADE_ENV).write_text(live)
    marker = (root / MARKER).read_text()
    again = run_block(root, upgrade_block())
    assert again.status == 0, again.output
    assert "Finishing: .env already names X" in again.output
    assert "systemctl start ibp-backup.service" not in again.steps
    assert (root / MARKER).read_text() == marker


@pytest.mark.parametrize("broken", ["no marker", "no file", "empty file"])
def test_finishing_needs_the_pinned_backup(tmp_path, broken):
    """.env naming the new tag without a pinned backup from before the
    switch stops the block before anything runs."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "retry")
    run_block(root, upgrade_block(), clean.index("docker compose pull") + 1)
    pinned = Path((root / MARKER).read_text().strip())
    if broken == "no marker":
        (root / MARKER).unlink()
    elif broken == "no file":
        pinned.unlink()
    else:
        pinned.write_text("")
    again = run_block(root, upgrade_block())
    assert again.status != 0
    assert "NOT UPGRADED: .env already names X, but" in again.output
    assert "nothing has changed" in again.output
    assert not [s for s in again.steps if s.startswith(("docker", "systemctl", "curl"))]


def test_finishing_refuses_another_exported_tag(tmp_path):
    """The exported-IBP_TAG check applies when finishing too."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "retry")
    run_block(root, upgrade_block(), clean.index("docker compose pull") + 1)
    again = run_block(root, upgrade_block(), env={"IBP_TAG": "v9.9.9"})
    assert again.status != 0
    assert "IBP_TAG is exported as 'v9.9.9'" in again.output
    assert not [s for s in again.steps if s.startswith("docker")]


def test_upgrade_run_again_before_the_switch_pins_its_own_backup(tmp_path):
    """Each attempt that gets to the backup pins its own and names it."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "retry")
    run_block(root, upgrade_block(), clean.index("mv .env.next-X .env") + 1)
    (root / MARKER).write_text("/elsewhere/before-X-first.db.zst\n")
    again = run_block(root, upgrade_block())
    assert again.status == 0, again.output
    pinned = Path((root / MARKER).read_text().strip())
    assert pinned.parent == root / "home" / "backups" / "ibp-pinned" and pinned.exists()


def test_upgrade_says_what_a_failure_after_the_switch_left(tmp_path):
    """Once .env names the new tag, a failure says so and how to go on."""
    clean = upgrade_steps(tmp_path)
    root = fresh_upgrade(tmp_path, "failed")
    result = run_block(root, upgrade_block(), clean.index("docker compose pull") + 1)
    assert result.status != 0
    assert ".env names X and the shipment timer is disabled" in result.output
    assert "run this block again to finish, or roll back" in result.output


def test_backups_never_prune_the_pinned_one():
    """ibp-backup prunes only its own data-*.db.zst, not ibp-pinned/."""
    script = (TOPLEVEL / "scripts" / "backup" / "ibp-backup").read_text()
    assert "LOCAL_DIR=${IBP_BACKUP_DIR:-/home/jstarr/backups/ibp}" in script
    assert 'ls -1 "$LOCAL_DIR"/data-*.db.zst' in script
    assert "ibp-pinned" not in script


@pytest.mark.parametrize(
    "edited, shell",
    [
        ("IBP_UID=${SHIP_UID}\n", {}),
        ("IBP_GID=jstarr\n", {}),
        ("", {"IBP_UID": "abc"}),
        ("IBP_TAG=v0.0.1\n", {}),
        # Compose would deploy an exported IBP_TAG instead of the file's.
        ("", {"IBP_TAG": "v9.9.9"}),
    ],
)
def test_upgrade_stops_on_a_bad_copy_before_changing_anything(tmp_path, edited, shell):
    """An interpolated or named IBP_UID/IBP_GID, a copy naming another tag,
    or another IBP_TAG exported, stops the block, saying so, before the
    timer is disabled; fixed, a re-run goes on."""
    root = fresh_upgrade(tmp_path, "bad")
    result = run_block(root, upgrade_block(), env={"NANO_EXTRA": edited, **shell})
    assert result.status != 0
    assert "NOT UPGRADED: " in result.output and "nothing has changed" in result.output
    assert not [
        s for s in result.steps if "systemctl disable" in s or "install -d" in s
    ]
    assert (root / UPGRADE_ENV).read_text() == "IBP_TAG=v1.0.0\n"
    assert run_block(root, upgrade_block()).status == 0


def test_upgrade_ignores_an_unexported_shell_variable(tmp_path):
    """Only an exported IBP_UID reaches docker compose, so only that one
    counts."""
    root = fresh_upgrade(tmp_path, "run")
    result = run_block(root, "IBP_UID=1009\n" + upgrade_block())
    assert result.status == 0, result.output
    assert "install -d -o 1000 -g 1000 -m 0750 /srv/ibp/logs" in result.steps


def backfill_block(readme: str) -> str:
    """The re-evaluation's backfill block (steps 6 and 7)."""
    (block,) = [b for b in code_blocks(readme) if "# 6. Re-evaluate" in b]
    return block


@pytest.mark.parametrize("readme", RUNBOOKS)
@pytest.mark.parametrize(
    "statuses, started",
    [([1, 75], False), ([1, 2], False), ([1, 1, 0], True), ([0], True)],
)
def test_timer_starts_only_after_a_complete_backfill(
    tmp_path, readme, statuses, started
):
    """With the sync exiting ``statuses`` in turn, the timer is started only
    if the last run exited 0; otherwise the block says the timer is still
    stopped and exits non-zero."""
    answers = tmp_path / "answers"
    answers.write_text("\n".join(map(str, statuses)) + "\n")
    result = run_block(
        sandbox(tmp_path), backfill_block(readme), env={"ANSWERS": str(answers)}
    )
    assert len([s for s in result.steps if s.startswith("ibp-shipment-sync")]) == len(
        statuses
    )
    assert ("systemctl start ibp-shipment-sync.timer" in result.steps) is started
    assert ("TIMER STILL STOPPED" in result.output) is not started
    assert (result.status == 0) is started


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_no_runbook_starts_the_timer_unconditionally(readme):
    """Every line starting or enabling the timer is a branch of an ``if``."""
    for block in code_blocks(readme):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        for previous, line in zip(["", *lines], lines):
            if (
                "ibp-shipment-sync.timer" in line
                and ("systemctl start" in line or "systemctl enable" in line)
                and not line.startswith("echo ")  # a message naming the command
            ):
                assert line.startswith("if ") or previous.endswith("then"), line


@pytest.mark.parametrize("readme", RUNBOOKS)
def test_runbook_waits_for_activating_runs_and_installs_the_wrapper_first(readme):
    """The upgrade runbooks wait for a run that is still activating and for
    any one-off sync container, and install the new wrapper and service
    before the backfill, which comes before starting the timer."""
    text = (TOPLEVEL / readme).read_text()
    steps = [
        "sudo systemctl stop ibp-shipment-sync.timer",
        "systemctl show -p ActiveState --value ibp-shipment-sync.service",
        "active | activating",
        "--filter label=com.docker.compose.oneoff=True",
        "install -D -m755 ibp-shipment-sync ~/.local/bin/ibp-shipment-sync",
        "sudo systemctl daemon-reload",
        "--since 2025-11-01",
        "sudo systemctl start ibp-shipment-sync.timer",
    ]
    start = text.find("# 1. Stop the timer")
    positions = [text.find(step, start) for step in steps]
    assert -1 not in positions, dict(zip(steps, positions))
    assert positions == sorted(positions)
