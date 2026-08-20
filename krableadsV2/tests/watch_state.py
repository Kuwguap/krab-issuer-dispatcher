"""Watch one user's live `states` row and print every change.

Used to confirm that the DEPLOYED bot (not a local process) actually applied a typed
edit: run it, type a line in Telegram, and the change shows up here within seconds.

Usage:  venv\\Scripts\\python.exe tests/watch_state.py <user_id> [seconds]
"""
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from supabase import create_client

ENV = dotenv_values(str(Path(__file__).resolve().parent.parent / ".env"))
WATCHED = ("name", "pending_price", "color", "car", "address", "city_state_zip",
           "vin", "insurance_company", "email", "driver_license_id")


def snapshot(client, user_id):
    r = client.table("states").select("*").eq("user_id", user_id).execute()
    if not r.data:
        return ("<no row>", {})
    row = r.data[0]
    d = row.get("data") or {}
    return (row.get("state"), {k: d.get(k) for k in WATCHED})


def main(user_id, seconds):
    client = create_client(ENV["SUPABASE_URL"], ENV["SUPABASE_KEY"])
    prev = snapshot(client, user_id)
    print(f"watching user {user_id} for {seconds}s")
    print(f"  start: state={prev[0]!r} {prev[1]}", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(6)
        cur = snapshot(client, user_id)
        if cur != prev:
            changed = {k: v for k, v in cur[1].items() if v != prev[1].get(k)}
            print(f"  CHANGE state={cur[0]!r} -> {changed}", flush=True)
            prev = cur
    print("watch finished", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 300)
