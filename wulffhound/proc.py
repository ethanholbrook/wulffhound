"""Run a chain job so that a timeout stops all of it.

subprocess.run(..., timeout=...) kills only the direct child. A chain job is a tree (sim2l-env -> python ->
run_sim2l.py -> papermill -> an ipykernel -> LAMMPS), so after a timeout the kernel and LAMMPS kept running,
orphaned, and their CPU was not counted (found in the TATB campaign, 2026-10-23). run_group starts the job in its
own session (process group) and, on timeout, sends SIGTERM and then SIGKILL to the whole group.
"""
import os
import signal
import subprocess
import time


def _kill_group(pgid, grace_s=10.0, proc=None):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        end = time.time() + (grace_s if sig == signal.SIGTERM else 0.0)
        while time.time() < end:
            if proc is not None:
                proc.poll()                  # reap the direct child, else it stays a zombie in the group
            try:
                os.killpg(pgid, 0)          # still alive?
            except ProcessLookupError:
                return
            time.sleep(0.2)


def run_group(cmd, timeout=None, **kw):
    """Like subprocess.run(cmd, timeout=timeout, **kw) with stdout/stderr captured as given by the caller, but the
    command runs in a new process group that is killed as a whole on timeout. Raises subprocess.TimeoutExpired
    (with the output collected so far) after the group is gone."""
    kw.pop('start_new_session', None)
    p = subprocess.Popen(cmd, start_new_session=True, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p.pid, proc=p)
        out, err = p.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    except BaseException:                    # e.g. KeyboardInterrupt: do not leave the tree behind
        _kill_group(p.pid, proc=p)
        p.communicate()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
