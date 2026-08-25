r"""A client who already has a portal account can still be insured.

Reported: "theres an option to create insurance Allow create multiple accounts with
same email — that was annoying creating email can't create account cuz email
exists", and "when user already has an account it says create failed, just create
the account again".

The portal answers 409 for a duplicate email and the bot treated that as a hard
failure, so the WHOLE insurance issue aborted with "Portal create failed" even
though the account it needed already existed. Every repeat customer hit it, and so
did a second car on a household email.

Run:  venv\Scripts\python.exe -m pytest tests/test_portal_duplicate_email.py -q
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import utils.tristatecoverage_api as tsc  # noqa: E402
from config import Config  # noqa: E402


def _call(status, body):
    Config.INTEGRATIONS_API_KEY = "k"
    resp = mock.MagicMock(status_code=status, content=b"x")
    resp.json.return_value = body
    with mock.patch.object(tsc.requests, "post", mock.MagicMock(return_value=resp)):
        return tsc.create_portal_client({"email": "a@b.com"})


class AnExistingAccountIsNotAFailureTest(unittest.TestCase):

    def test_409_succeeds(self):
        r = _call(409, {"error": "Portal account already exists for this email"})
        self.assertTrue(r.ok)
        self.assertIsNone(r.error)

    def test_it_says_the_account_was_already_there(self):
        r = _call(409, {"error": "already exists"})
        self.assertTrue((r.payload or {}).get("alreadyExisted"))

    def test_the_other_spellings_the_portal_uses(self):
        for status, body in (
            (400, {"error": "A user with this email already exists"}),
            (400, {"message": "email already registered"}),
            (200, {"ok": False, "message": "duplicate email"}),
            (422, {"code": "user_already_exists"}),
            (200, {"ok": False, "error": "This client already has an account"}),
        ):
            with self.subTest(status=status, body=body):
                self.assertTrue(_call(status, body).ok, body)

    def test_a_clean_create_still_succeeds(self):
        r = _call(200, {"ok": True, "clientId": "c1"})
        self.assertTrue(r.ok)
        self.assertFalse((r.payload or {}).get("alreadyExisted"))


class RealFailuresStillFailTest(unittest.TestCase):
    """This must not become "everything is fine" — a bad key is still a bad key."""

    def test_a_bad_api_key(self):
        r = _call(401, {"error": "bad key"})
        self.assertFalse(r.ok)
        self.assertIn("INTEGRATIONS_API_KEY", r.error)

    def test_a_server_error(self):
        self.assertFalse(_call(500, {"error": "boom"}).ok)

    def test_a_validation_error_that_is_not_a_duplicate(self):
        r = _call(400, {"error": "vehicleVin must be 17 characters"})
        self.assertFalse(r.ok)
        self.assertIn("17 characters", r.error)

    def test_the_portal_being_unconfigured(self):
        r = _call(503, {"error": "missing key"})
        self.assertFalse(r.ok)

    def test_an_empty_body_is_not_read_as_a_duplicate(self):
        self.assertFalse(_call(500, {}).ok)


class TheClientIsNeverGivenAPasswordThatFailsTest(unittest.TestCase):
    """The portal keeps an EXISTING account's password rather than taking ours.

    The bot generates Temp#A9 and prints it as the login. On an account that
    already existed createUser never ran, so that password does not work — handing
    it over sends the client to a login screen that rejects them."""

    def _block(self, unchanged):
        import bot
        return bot._insurance_login_block("POL123", "a@b.com", "Temp#A9", unchanged)

    def test_a_new_account_gets_its_password(self):
        self.assertIn("Temp#A9", self._block(False))

    def test_an_existing_account_does_not(self):
        block = self._block(True)
        self.assertNotIn("Temp#A9", block)
        self.assertIn("already had an account", block)

    def test_it_says_how_to_recover_it(self):
        self.assertIn("Reset at", self._block(True))

    def test_the_email_and_policy_are_shown_either_way(self):
        for unchanged in (True, False):
            with self.subTest(unchanged=unchanged):
                block = self._block(unchanged)
                self.assertIn("a@b.com", block)
                self.assertIn("POL123", block)

    def test_the_server_reports_it(self):
        """The route must tell us, or the bot cannot know to hold the password back."""
        route = Path(r"C:/Users/tatia/Downloads/b_H821T7ehlpo/app/api/integrations/clients/route.ts")
        if not route.exists():
            self.skipTest("the tristatecoverage project is not on this machine")
        src = route.read_text(encoding="utf-8")
        self.assertIn("passwordUnchanged", src)

    def test_an_existing_client_keeps_their_other_cars(self):
        """delete().eq('user_id') is right for a new account and destructive on a
        real one — it would remove the car they already had insured."""
        action = Path(r"C:/Users/tatia/Downloads/b_H821T7ehlpo/app/actions/admin-create-client.ts")
        if not action.exists():
            self.skipTest("the tristatecoverage project is not on this machine")
        src = action.read_text(encoding="utf-8")
        self.assertIn(".eq('vin', input.vin)", src)
        self.assertIn(".eq('policy_number', input.policyNumber)", src)


class TheDuplicateDetectorTest(unittest.TestCase):

    def test_it_reads_every_field_the_portal_might_use(self):
        for body in ({"error": "already exists"}, {"message": "Already Registered"},
                     {"code": "EMAIL_EXISTS"}, {"detail": "duplicate"}):
            with self.subTest(body=body):
                self.assertTrue(tsc._looks_like_duplicate(body), body)

    def test_it_does_not_fire_on_unrelated_text(self):
        for body in ({"error": "vin invalid"}, {"message": "server error"},
                     {}, None, "already exists"):
            with self.subTest(body=body):
                self.assertFalse(tsc._looks_like_duplicate(body), body)


if __name__ == "__main__":
    unittest.main()
