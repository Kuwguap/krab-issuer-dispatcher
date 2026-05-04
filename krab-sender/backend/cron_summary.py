"""
Weekly summary job for Krab Dispatch.

Intended to be triggered by an external scheduler (cron, serverless scheduled
function, etc.) at **Saturday 12:00 AM America/New_York time**, as specified
in the roadmap.
"""

from datetime import datetime, timezone

from .db import init_db
from .repository import get_rolling_summary_ny


def run_weekly_summary() -> None:
    """
    Generate and print a rolling 7‑day summary in NY time.

    In a real deployment, this could:
    - Send the summary via email to admins,
    - Store it in a 'summaries' table,
    - Or expose it through an admin dashboard.
    """
    init_db()

    now_utc = datetime.now(timezone.utc)
    summary = get_rolling_summary_ny(days=7, reference_utc=now_utc)

    print("=== Krab Dispatch 7‑Day Summary (America/New_York) ===")
    print(f"Period: {summary['period_start_ny']} -> {summary['period_end_ny']}")
    print(f"Total: {summary['total_transactions']}")
    print(f"Delivered: {summary['delivered']}")
    print(f"Pending: {summary['pending']}")
    print(f"Failed: {summary['failed']}")
    print("-----------------------------------------------------------------")
    for item in summary["items"]:
        print(
            f"{item['timestamp_ny']} | {item['telegram_name']} "
            f"(@{item['telegram_handle']}) | {item['filename']} "
            f"| {item['delivery_status']}"
        )
    print("=== End Weekly Summary ===")


if __name__ == "__main__":
    run_weekly_summary()


