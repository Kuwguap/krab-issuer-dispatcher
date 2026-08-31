r"""The page a supervisor reaches from the "email tag to the client" message.

That button is the only thing standing between a generated tag and a client's
inbox, so this covers what it refuses as carefully as what it does.

Run:  venv\Scripts\python.exe -m pytest tests_dispatch_web/test_tagsend_portal.py -q
"""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-key")

import admin_dashboard as ad  # noqa: E402

LEAD_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class _Table:
    """Just enough Supabase to answer the two calls the page makes.

    AdminDatabase is a reporting wrapper with no get_lead_by_id — the page
    queries db.client directly, the way the receipt portal does, so that is
    what has to be faked.
    """

    def __init__(self, rows, update_rows):
        self._rows = rows
        self._update_rows = update_rows
        self._mode = ""
        self.updates = []

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def update(self, patch):
        self.updates.append(patch)
        self._mode = "update"
        return self

    def execute(self):
        if self._mode == "update":
            self._mode = ""
            return mock.MagicMock(data=list(self._update_rows))
        return mock.MagicMock(data=list(self._rows))


def _fake_db(lead=None, update_rows=(1,)):
    """(patcher_target_value, table) — patch onto ad.db.client."""
    table = _Table([lead] if lead else [], update_rows)
    client = mock.MagicMock()
    client.table.return_value = table
    return client, table


def _lead(**over):
    lead = {"id": LEAD_ID, "reference_id": "REF1", "email": "client@example.com",
            "vehicle_details": "Magnolia Diaz\n-\n-"}
    lead.update(over)
    return lead


@pytest.fixture
def client():
    return ad.app.test_client()


def _url(vehicle=1):
    return "/tagsend/" + ad.tagsend_token(LEAD_ID, vehicle)


# ------------------------------------------------------------------ the token

def test_the_token_round_trips():
    assert ad.tagsend_from_token(ad.tagsend_token(LEAD_ID, 2)) == (LEAD_ID, 2)


def test_a_forged_mac_is_refused():
    assert ad.tagsend_from_token(f"{LEAD_ID}.1.deadbeefdeadbeefdeadbeef") is None


def test_a_receipt_token_is_not_a_send_permission():
    """Different powers over the same lead id. The message prefix keeps them
    apart, so a receipt link can never be replayed as permission to email."""
    assert ad.tagsend_from_token(ad.receipt_token(LEAD_ID)) is None


def test_a_tagsend_token_is_not_a_receipt_link():
    assert ad.receipt_lead_from_token(ad.tagsend_token(LEAD_ID, 1)) is None


def test_each_car_has_its_own_token():
    assert ad.tagsend_token(LEAD_ID, 1) != ad.tagsend_token(LEAD_ID, 2)
    assert ad.tagsend_from_token(ad.tagsend_token(LEAD_ID, 2))[1] == 2


@pytest.mark.parametrize("bad", ["", "garbage", f"{LEAD_ID}.1", f"{LEAD_ID}.x.abc",
                                 "..", f"{LEAD_ID}..abc"])
def test_malformed_tokens_are_refused_not_crashed(bad):
    assert ad.tagsend_from_token(bad) is None


# ------------------------------------------------------------------- the page

def test_a_bad_link_is_a_404_page(client):
    r = client.get("/tagsend/garbage")
    assert r.status_code == 404
    assert r.mimetype == "text/html"
    assert "not valid" in r.get_data(as_text=True)


def test_a_missing_lead_is_a_404_page(client):
    fake, _t = _fake_db(None)
    with mock.patch.object(ad.db, "client", fake):
        r = client.get(_url())
    assert r.status_code == 404
    assert "no longer exists" in r.get_data(as_text=True)


def test_the_get_shows_who_it_would_go_to_and_sends_nothing(client):
    fake, table = _fake_db(_lead())
    with mock.patch.object(ad.db, "client", fake):
        r = client.get(_url())
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Magnolia Diaz" in body
    assert "client@example.com" in body
    assert "REF1" in body
    assert table.updates == [], "a GET must never send — mail clients prefetch links"


def test_the_post_records_the_approval(client):
    fake, table = _fake_db(_lead())
    with mock.patch.object(ad.db, "client", fake):
        r = client.post(_url())
    assert r.status_code == 200
    assert "on its way" in r.get_data(as_text=True).lower()
    assert len(table.updates) == 1
    patch = table.updates[0]
    assert patch["tag_email_approved_at"]
    assert "tag_emailed_at" not in patch, "the client has not been sent anything yet"


def test_the_page_never_builds_the_tag():
    """The bot owns the builder: it allocates and persists a plate, and a second
    implementation here would eventually send a different document."""
    src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
    body = src.split("def _tagsend_deliver(", 1)[1].split("\n_TAGSEND_PAGE", 1)[0]
    assert "build_tag_pdf" not in body
    assert "send_insurance_card_email" not in body


def test_an_already_sent_tag_is_not_sent_again(client):
    fake, table = _fake_db(_lead(tag_emailed_at="2026-08-30T10:00:00Z"))
    with mock.patch.object(ad.db, "client", fake):
        r = client.post(_url())
    assert r.status_code == 200
    assert "already" in r.get_data(as_text=True).lower()
    assert table.updates == []


def test_an_already_approved_tag_is_not_approved_twice(client):
    fake, table = _fake_db(_lead(tag_email_approved_at="2026-08-30T10:00:00Z"))
    with mock.patch.object(ad.db, "client", fake):
        client.post(_url())
    assert table.updates == []


def test_a_lead_with_no_address_cannot_be_approved(client):
    fake, table = _fake_db(_lead(email=""))
    with mock.patch.object(ad.db, "client", fake):
        r = client.post(_url())
    assert r.status_code == 400
    assert table.updates == []


def test_an_un_migrated_database_says_so_rather_than_lying(client):
    """The columns do not exist in production yet. An approval that did not
    stick must not render as one that did."""
    fake, _t = _fake_db(_lead(), update_rows=())
    with mock.patch.object(ad.db, "client", fake):
        r = client.post(_url())
    assert r.status_code == 502
    assert "migration_tag_email" in r.get_data(as_text=True)


def test_the_recipient_never_comes_from_the_url():
    """The token carries a lead and a car — never an address."""
    src = (ROOT / "admin_dashboard.py").read_text(encoding="utf-8")
    body = src.split("def tagsend_portal(", 1)[1].split("\n@app.route", 1)[0]
    assert 'lead.get("email")' in body
    assert "request.args" not in body
    assert "request.form" not in body
