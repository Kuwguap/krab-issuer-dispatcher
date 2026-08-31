-- Email the temp tag to the client — a per-lead switch, like insurance.
--
-- The issuer flips "📧 Email tag to client" on the review card. When the tag
-- PDF goes out to the team it also goes to the client's inbox, and the
-- /receipts board moves to "Tag emailed" — the one stop on that ladder that
-- had no automatic trigger, because nothing in this system emailed a tag.
--
-- tag_emailed_at is the idempotency guard: a tag re-sent to the group must not
-- mail the client a second copy. Extra cars keep their own stamp inside the
-- extra_vehicles blob, exactly as their insurance does.
--
-- Run this in the Supabase SQL editor (idempotent, safe to re-run).

alter table leads
    add column if not exists wants_tag_email boolean default false,
    add column if not exists tag_emailed_at  timestamptz,
    add column if not exists tag_email_error text;

comment on column leads.wants_tag_email is
    'Issuer asked for the temp tag to be emailed to the client.';
comment on column leads.tag_emailed_at is
    'When the tag PDF was emailed to the client (car 1). NULL means never.';

-- ── Supervisor approval (added later) ───────────────────────────────────
-- The toggle no longer mails the client directly. Every supervisory address
-- gets the tag attached and a button; nothing reaches the client until one of
-- them presses it. tag_email_asked_at stops the same car being asked about
-- twice; tag_email_approved_at is what the bot's sweep looks for.

alter table leads
    add column if not exists tag_email_asked_at    timestamptz,
    add column if not exists tag_email_approved_at timestamptz;

comment on column leads.tag_email_asked_at is
    'The supervisors were emailed for approval to send this tag to the client.';
comment on column leads.tag_email_approved_at is
    'A supervisor released it. The bot sends the client their copy on the next sweep.';

create index if not exists idx_leads_tag_email_awaiting
    on leads (tag_email_approved_at)
    where tag_email_approved_at is not null and tag_emailed_at is null;
