"""Stop shared Serena servers that have had no MCP traffic for a while.

Idle is measured from the activity file written by serena-shared and the
stdio proxy on client-to-server messages. Open TCP sockets from abandoned
clients do not keep a server alive.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

SHARED_DIR = Path(os.environ.get("SERENA_SHARED_DIR", str(Path.home() / ".serena" / "shared")))
SERVERS_DIR = SHARED_DIR / "servers"
LOCK_PATH = SHARED_DIR / "reaper.lock"
IDLE_SECONDS = int(os.environ.get("SERENA_SHARED_IDLE_SECONDS", "1800"))
POLL_SECONDS = int(os.environ.get("SERENA_SHARED_REAPER_POLL", "60"))


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _parse_record(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key] = value.strip().strip("'\"")
    return data


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _activity_stamp(record: Path) -> float:
    activity = Path(str(record) + ".activity")
    if activity.is_file():
        return activity.stat().st_mtime
    return record.stat().st_mtime


def _signal_group_or_pid(pid: int, sig: int) -> bool:
    try:
        os.killpg(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        pass
    try:
        os.kill(pid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def _stop(pid: int) -> None:
    if not _signal_group_or_pid(pid, signal.SIGTERM):
        return
    for _ in range(10):
        if not _pid_alive(pid):
            return
        time.sleep(0.3)
    _signal_group_or_pid(pid, signal.SIGKILL)


def reap_once(now: float | None = None) -> int:
    if not SERVERS_DIR.is_dir():
        return 0
    now = time.time() if now is None else now
    stopped = 0
    for record in SERVERS_DIR.iterdir():
        if record.suffix == ".activity" or not record.is_file():
            continue
        try:
            data = _parse_record(record)
            pid = int(data.get("pid") or "0")
        except (OSError, ValueError):
            continue
        if pid <= 0 or not _pid_alive(pid):
            record.unlink(missing_ok=True)
            Path(str(record) + ".activity").unlink(missing_ok=True)
            continue
        idle = now - _activity_stamp(record)
        if idle < IDLE_SECONDS:
            continue
        project = data.get("project", "?")
        context = data.get("context", "?")
        _log(f"stopping idle {context} {project} pid={pid} idle={int(idle)}s")
        _stop(pid)
        record.unlink(missing_ok=True)
        Path(str(record) + ".activity").unlink(missing_ok=True)
        stopped += 1
    return stopped


def main() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    once = "--once" in sys.argv
    _log(f"reaper start idle={IDLE_SECONDS}s poll={POLL_SECONDS}s")
    while True:
        try:
            reap_once()
        except Exception as exc:
            _log(f"reaper error: {exc}")
        if once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
