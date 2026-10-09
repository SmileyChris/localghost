"""PID 1 for an isolated run.

pasta starts its command as the first process of a new PID namespace, and
the kernel drops any signal PID 1 has no handler for. A dev server that
relies on the default SIGTERM would survive the end of its own run. This
stands in as PID 1 instead: the application runs as an ordinary process
in the same process group, so localghost's group signals reach it
directly, and this exits once it has.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def main(command: list[str]) -> int:
    child = subprocess.Popen(command)
    # A handler rather than SIG_IGN: the group signal is meant for the
    # child, and an ignored disposition would be inherited by anything
    # this process went on to exec.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: None)
    while True:
        # Orphans in the namespace reparent here, so reap them all.
        pid, status = os.waitpid(-1, 0)
        if pid == child.pid:
            code = os.waitstatus_to_exitcode(status)
            return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
