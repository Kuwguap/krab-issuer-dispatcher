"""Prove Sentry is wired up AND that a customer cannot leak through it.

Run once after setting SENTRY_DSN, on a developer machine or on Render:

    venv\\Scripts\\python.exe verify_sentry.py

It raises a synthetic error stuffed with the exact shapes of data this business
handles -- a legal name, a home address, a VIN, a plate, a licence number, a
policy number, a phone and an email -- then prints the event as it would leave
the process, and only then transmits it.

The data below is INVENTED. Never point this at a real lead: the whole purpose
is to watch the redaction work, and a real customer in a test event is the thing
the redaction exists to prevent.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.observability import init_sentry, _before_send   # noqa: E402

# Invented, and deliberately in the shapes the scrubber must recognise.
FAKE = {
    "name": "Charles Jones",
    "address": "9 hibiscus Lane",
    "city_state_zip": "Monticello New York 13701",
    "vin": "1N4AL3AP0HC166043",
    "plate": "000001V",
    "driver_license": "J12345678901234",
    "insurance_policy_number": "0407306000",
    "phone_number": "845-423-9476",
    "email": "charles.jones@example.com",
}


def _leaks(blob: str) -> list:
    """Any invented value still readable in what we are about to transmit."""
    found = []
    for key, value in FAKE.items():
        # The address survives as separate tokens legitimately (a city name is
        # not identifying on its own); check the whole value, which is.
        if value and value in blob:
            found.append(f"{key}={value}")
    return found


def main() -> int:
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        print("SENTRY_DSN is not set — nothing to verify.")
        print("  PowerShell:  $env:SENTRY_DSN = '<your dsn>'")
        return 2

    print(f"DSN host: {dsn.split('@')[-1] or '?'}")
    if not init_sentry("verify"):
        print("init_sentry returned False — sentry-sdk missing or init failed.")
        return 1
    print("init_sentry: on")

    # Build the event the way a real crash would: PII inside the exception
    # message, not politely in a labelled field.
    try:
        raise ValueError(
            f"sentry install check — lead for {FAKE['name']} at {FAKE['address']}, "
            f"{FAKE['city_state_zip']}, VIN {FAKE['vin']}, plate {FAKE['plate']}, "
            f"DL {FAKE['driver_license']}, policy {FAKE['insurance_policy_number']}, "
            f"phone {FAKE['phone_number']}, email {FAKE['email']}"
        )
    except ValueError as exc:
        event = {
            "message": str(exc),
            "extra": dict(FAKE),
            "request": {"url": f"https://example.test/lead?vin={FAKE['vin']}"},
        }
        # _before_send, NOT _scrub: the gate collects every value sitting under
        # a sensitive key FIRST and feeds them to the text pass, which is how a
        # name in prose gets caught. Calling _scrub alone skips that and tests
        # something production never runs.
        scrubbed = _before_send(event, None)
        blob = json.dumps(scrubbed)

        print("\n--- what would leave this process ---")
        print(json.dumps(scrubbed, indent=2)[:1400])

        leaked = _leaks(blob)
        if leaked:
            print("\nFAILED — these values survived scrubbing:")
            for item in leaked:
                print("   " + item)
            print("\nNothing was transmitted.")
            return 1
        print("\nNo invented customer value survived scrubbing.")

        import sentry_sdk
        sentry_sdk.capture_exception(exc)
        sentry_sdk.flush(timeout=10)
        print("Test event sent. It should appear in Sentry within a minute,")
        print("tagged component=verify, with every customer value redacted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
