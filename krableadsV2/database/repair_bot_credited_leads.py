#!/usr/bin/env python
"""Give back the leads the bot took credit for.

Some lead-creation paths resolved the ENTRANT from the message they were handed.
A review-card Submit hands back the bot's own message, so `telegram_username`
was written as the bot -- 207 leads on this database, from 2026-05-17 to
2026-08-27, every one of them somebody's real work sitting under
"KrabDispatchBot" on the leaderboard.

The rows are repairable because only the display fields were wrong: `user_id`
was correct throughout. This resolves each distinct user_id through Telegram and
writes back the username and display name.

The bug itself was fixed on 2026-08-28 (an is_bot guard plus a get_chat
fallback), and no lead has been mis-credited since. This is only the cleanup.

    python database/repair_bot_credited_leads.py            # dry run
    python database/repair_bot_credited_leads.py --apply    # write

Idempotent: it only ever touches rows still credited to the bot, so running it
twice is a no-op. Ids Telegram cannot resolve (the person never opened a DM, or
blocked the bot) are reported and left alone rather than guessed at.
"""
import argparse
import collections
import os
import sys

import httpx
from dotenv import load_dotenv

BOT_HANDLES = {"krabdispatchbot", "krabissuerbot"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    load_dotenv()
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not (url and key and token):
        print("need SUPABASE_URL, SUPABASE_KEY and TELEGRAM_BOT_TOKEN")
        return 2
    h = {"apikey": key, "Authorization": "Bearer " + key}

    rows = []
    for handle in sorted(BOT_HANDLES):
        r = httpx.get(f"{url}/rest/v1/leads",
                      params={"select": "id,reference_id,user_id",
                              "telegram_username": f"ilike.{handle}",
                              "limit": "2000"},
                      headers=h, timeout=60)
        r.raise_for_status()
        rows += r.json()
    if not rows:
        print("nothing credited to the bot — already repaired.")
        return 0

    by_uid = collections.defaultdict(list)
    for row in rows:
        if row.get("user_id"):
            by_uid[str(row["user_id"])].append(row)
    print(f"{len(rows)} lead(s) credited to the bot, "
          f"across {len(by_uid)} distinct user_id(s)\n")

    fixed = unresolved = 0
    for uid, leads in sorted(by_uid.items(), key=lambda kv: -len(kv[1])):
        who = httpx.get(f"https://api.telegram.org/bot{token}/getChat",
                        params={"chat_id": uid}, timeout=25).json()
        if not who.get("ok"):
            print(f"  SKIP  {len(leads):>4} lead(s)  {uid}  "
                  f"({who.get('description')})")
            unresolved += len(leads)
            continue
        c = who["result"]
        username = (c.get("username") or "").strip()
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x).strip()
        if not username and not name:
            print(f"  SKIP  {len(leads):>4} lead(s)  {uid}  (no username or name)")
            unresolved += len(leads)
            continue
        patch = {}
        if username:
            patch["telegram_username"] = username
        if name:
            patch["telegram_name"] = name
        print(f"  {'FIX ' if args.apply else 'WOULD'}  {len(leads):>4} lead(s)  "
              f"{uid}  ->  @{username or '-'}  {name!r}")
        if args.apply:
            for lead in leads:
                w = httpx.patch(f"{url}/rest/v1/leads",
                                params={"id": f"eq.{lead['id']}"},
                                headers={**h, "Content-Type": "application/json",
                                         "Prefer": "return=minimal"},
                                json=patch, timeout=30)
                if w.status_code >= 300:
                    print(f"        ! {lead.get('reference_id')}: "
                          f"{w.status_code} {w.text[:120]}")
                    continue
                fixed += 1
        else:
            fixed += len(leads)

    print(f"\n{'repaired' if args.apply else 'would repair'}: {fixed}"
          f"   left alone (unresolvable): {unresolved}")
    if not args.apply:
        print("dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
