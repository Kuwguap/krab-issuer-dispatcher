"""One-shot backfill: parse client fields for existing transactions.

Rows where all four independent-registration columns are NULL get a local
parse (filename + client_details — no Supabase). Run manually:

    python -m backend.backfill_client_fields --dry-run
    python -m backend.backfill_client_fields
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db import SessionLocal, TransactionORM, init_db  # noqa: E402
from bot.client_capture import capture_client_fields  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument("--limit", type=int, default=0, help="max rows to process (0 = all)")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    try:
        q = session.query(TransactionORM).filter(
            TransactionORM.client_name.is_(None),
            TransactionORM.price.is_(None),
            TransactionORM.client_phone.is_(None),
            TransactionORM.client_email.is_(None),
        )
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        print(f"{len(rows)} row(s) with no captured client fields")
        changed = 0
        for r in rows:
            captured = capture_client_fields(
                r.filename or "", r.client_details or "", r.recipient_name, r.recipient_email
            )
            if not any(captured.values()):
                continue
            changed += 1
            if args.dry_run:
                print(f"  {r.id[:8]} {r.filename!r} -> {captured}")
                continue
            if captured["client_name"]:
                r.client_name = captured["client_name"]
            if captured["price"]:
                r.price = captured["price"]
            if captured["client_phone"]:
                r.client_phone = captured["client_phone"]
            if captured["client_email"]:
                r.client_email = captured["client_email"]
        if not args.dry_run:
            session.commit()
        print(f"{'would update' if args.dry_run else 'updated'} {changed} row(s)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
