#!/usr/bin/env python3
"""Relaunch protopandaemu.launcher on a real screen whenever its source
changes, for live GUI iteration while the GUI is being edited.

Also respawns if the window is closed manually, so it stays up between
restarts without needing to be started by hand again. Ctrl+C to stop
watching (does not kill an already-open launcher window).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH_FILES = [
    ROOT / "protopandaemu" / "launcher.py",
    ROOT / "protopandaemu" / "supervisor.py",
]
DISPLAY = os.environ.get("DISPLAY", ":1")
POLL_SECONDS = 0.5


def file_sig() -> tuple:
    sig = []
    for f in WATCH_FILES:
        try:
            sig.append(f.stat().st_mtime_ns)
        except OSError:
            sig.append(None)
    return tuple(sig)


def launch() -> subprocess.Popen:
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    argv = [sys.executable, "-m", "protopandaemu.launcher"]
    if len(sys.argv) > 1:
        argv += sys.argv[1:]
    return subprocess.Popen(argv, cwd=str(ROOT), env=env)


def main() -> int:
    proc = launch()
    last_sig = file_sig()
    print(f"[dev-watch] launched pid={proc.pid} on DISPLAY={DISPLAY}", flush=True)
    try:
        while True:
            time.sleep(POLL_SECONDS)
            sig = file_sig()
            if sig != last_sig:
                print("[dev-watch] source changed, restarting...", flush=True)
                last_sig = sig
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                proc = launch()
                print(f"[dev-watch] launched pid={proc.pid}", flush=True)
            elif proc.poll() is not None:
                print("[dev-watch] window closed, relaunching...", flush=True)
                proc = launch()
                last_sig = file_sig()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
