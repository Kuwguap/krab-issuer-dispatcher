#!/usr/bin/env python3
"""
Run the full local stack from this folder (unity):

  python start_bots.py

Starts (in order):
  1. Krab Dispatch API     — FastAPI (uvicorn), default http://127.0.0.1:8000
  2. Krab Issuer admin     — Flask (`admin_dashboard.py`), default port 5000
  3. Krab Issuer Telegram bot (`krableadsV2/bot.py`)
  4. Krab Dispatch Telegram bot (`krab-sender`)

Environment:
  DISPATCH_API_PORT   — uvicorn port (default 8000). Bot uses API_BASE_URL from
                        krab-sender/.env; if unset, this script sets
                        API_BASE_URL=http://127.0.0.1:<DISPATCH_API_PORT> for children.
  ADMIN_PORT          — Flask issuer admin port (default 5000).
  START_ISSUER_ADMIN  — set to 0 / false / no to skip the Flask admin process.

Requires dependencies installed in each project and `.env` files as usual.
Press Ctrl+C to stop all processes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KRAB_ISSUER_DIR = ROOT / "krableadsV2"
KRAB_DISPATCH_DIR = ROOT / "krab-sender"


def _truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _dispatch_api_port() -> int:
    for key in ("DISPATCH_API_PORT", "API_PORT"):
        v = os.getenv(key)
        if v and v.strip().isdigit():
            return int(v.strip())
    return 8000


def _issuer_admin_port() -> int:
    v = os.getenv("ADMIN_PORT")
    if v and v.strip().isdigit():
        return int(v.strip())
    return 5000


_LOCK_PATH = ROOT / ".start_bots.lock"


def _acquire_local_lock() -> bool:
    """
    Prevent accidentally running two copies of `start_bots.py` on the same
    machine, which would double-poll each bot token and trigger
    'Conflict: terminated by other getUpdates request' on Telegram.
    """
    try:
        if _LOCK_PATH.exists():
            raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
            pid = int(raw) if raw.isdigit() else 0
            if pid > 0 and _pid_running(pid):
                print(
                    f"start_bots already running (pid={pid}). "
                    "Stop it first, or delete .start_bots.lock if stale.",
                    file=sys.stderr,
                )
                return False
        _LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception as e:
        print(f"Warning: could not write local lockfile: {e}", file=sys.stderr)
        return True  # don't hard-block on FS quirks


def _release_local_lock() -> None:
    try:
        if _LOCK_PATH.exists():
            raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                _LOCK_PATH.unlink()
    except Exception:
        pass


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL,
            ).decode(errors="ignore")
            return f" {pid} " in out or f"\t{pid}\t" in out or str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def main() -> int:
    if not KRAB_ISSUER_DIR.is_dir():
        print(f"Missing folder: {KRAB_ISSUER_DIR}", file=sys.stderr)
        return 1
    if not KRAB_DISPATCH_DIR.is_dir():
        print(f"Missing folder: {KRAB_DISPATCH_DIR}", file=sys.stderr)
        return 1

    if not _acquire_local_lock():
        return 2

    dispatch_port = _dispatch_api_port()
    admin_port = _issuer_admin_port()
    start_issuer_admin = _truthy(os.getenv("START_ISSUER_ADMIN", "1"))

    child_env = os.environ.copy()
    api_base = (child_env.get("API_BASE_URL") or "").strip()
    if not api_base:
        api_base = f"http://127.0.0.1:{dispatch_port}"
        child_env["API_BASE_URL"] = api_base

    procs: list[tuple[str, subprocess.Popen]] = []

    procs.append(
        (
            "krab-dispatch-api",
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(dispatch_port),
                ],
                cwd=str(KRAB_DISPATCH_DIR),
                env=child_env,
            ),
        )
    )

    if start_issuer_admin:
        issuer_admin_env = child_env.copy()
        issuer_admin_env.pop("PORT", None)
        issuer_admin_env["ADMIN_PORT"] = str(admin_port)
        procs.append(
            (
                "krab-issuer-admin",
                subprocess.Popen(
                    [sys.executable, "admin_dashboard.py"],
                    cwd=str(KRAB_ISSUER_DIR),
                    env=issuer_admin_env,
                ),
            )
        )
    else:
        print("Skipping krab-issuer-admin (START_ISSUER_ADMIN disabled).")

    for name, p in procs:
        print(f"Started {name} (pid={p.pid})")

    print(f"\n  Dispatch API:  {api_base}")
    if start_issuer_admin:
        print(f"  Issuer admin: http://127.0.0.1:{admin_port}/")
    print("  Waiting for APIs to listen…\n")
    time.sleep(2.0)

    procs.append(
        (
            "krab-issuer",
            subprocess.Popen(
                [sys.executable, "bot.py"],
                cwd=str(KRAB_ISSUER_DIR),
                env=child_env,
            ),
        )
    )
    procs.append(
        (
            "krab-dispatch",
            subprocess.Popen(
                [sys.executable, "-m", "bot.main"],
                cwd=str(KRAB_DISPATCH_DIR),
                env=child_env,
            ),
        )
    )

    for name, p in procs[-2:]:
        print(f"Started {name} (pid={p.pid})")
    print("Press Ctrl+C to stop everything.\n")

    try:
        while True:
            for name, p in procs:
                code = p.poll()
                if code is not None:
                    print(f"{name} exited with code {code}")
                    return int(code if code is not None else 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping all processes…")
        return 0
    finally:
        for name, p in procs:
            if p.poll() is None:
                p.terminate()
        for name, p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                print(f"Force kill: {name}", file=sys.stderr)
                p.kill()
        _release_local_lock()


if __name__ == "__main__":
    raise SystemExit(main())
