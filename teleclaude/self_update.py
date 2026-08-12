"""Process management: PID file, kill previous instance, restart via os.execv."""

import atexit
import logging
import os
import signal
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _under_launchd() -> bool:
    """True when the process was started by macOS launchd.

    launchd injects XPC_SERVICE_NAME into the child env. When set, launchd already
    guarantees single-instance via KeepAlive, so the PID-file dance is redundant
    (and risks SIGTERMing a healthy sibling if state ever skews).
    """
    name = os.environ.get("XPC_SERVICE_NAME", "")
    return bool(name) and name != "0"


def kill_previous(pidfile: str):
    """Kill any previous bot instance using a PID file, then write our own PID.

    Call this at startup before anything else. Skipped under launchd - launchd's
    KeepAlive already guarantees single-instance, so the PID-file logic is dead
    code there and only adds a foot-gun.
    """
    if _under_launchd():
        log.info("Running under launchd (XPC_SERVICE_NAME=%s); skipping kill_previous",
                 os.environ.get("XPC_SERVICE_NAME"))
        return

    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
            if old_pid == os.getpid():
                # Post-execv restart: pidfile is ours, don't suicide.
                log.info("Pidfile matches our PID (%d), assuming execv restart", old_pid)
            else:
                os.kill(old_pid, signal.SIGTERM)
                log.info("Killed previous instance (PID %d)", old_pid)
        except (ProcessLookupError, ValueError):
            pass  # already dead or corrupt file
        except PermissionError:
            log.warning("Cannot kill PID in pidfile (permission denied)")

    # Write our own PID
    Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(pidfile) and os.remove(pidfile))


def restart():
    """Replace the current process with a fresh Python invocation.

    Uses os.execv to keep the same PID and arguments.
    """
    log.info("Restarting process...")
    os.execv(sys.executable, [sys.executable] + sys.argv)
