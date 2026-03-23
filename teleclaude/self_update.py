"""Process management: PID file, kill previous instance, restart via os.execv."""

import atexit
import logging
import os
import signal
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def kill_previous(pidfile: str):
    """Kill any previous bot instance using a PID file, then write our own PID.

    Call this at startup before anything else.
    """
    if os.path.exists(pidfile):
        try:
            old_pid = int(open(pidfile).read().strip())
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
