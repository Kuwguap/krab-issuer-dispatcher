-- Deleting a lead, without destroying the evidence.
--
-- A lead that was entered by mistake, twice, as a test, or that the client
-- cancelled has to leave every board and every count. It must NOT leave the
-- database: money may already have been taken against it, a driver may already
-- owe a receipt for it, and a row that is gone cannot answer either question.
--
-- So a deletion is a flag plus its reason and its author. Everything that reads
-- leads hides a flagged row; nothing has to be reconstructed to undo one.
--
-- Safe to run more than once.

alter table leads
    add column if not exists deleted_at     timestamptz,
    add column if not exists deleted_reason text,
    add column if not exists deleted_by     text;

comment on column leads.deleted_at is
    'Set = the lead is deleted: hidden from /receipts, the leaderboard, usage, '
    'receipts owed and the driver suspension count. Clear it to restore.';
comment on column leads.deleted_reason is
    'Why: duplicate, mistake, test, cancelled, or free text.';
comment on column leads.deleted_by is
    'Who deleted it, as the board knows them.';

-- Every board read now asks "and not deleted", so this is the shape of that
-- question. Partial: the deleted rows are the rare ones.
create index if not exists idx_leads_not_deleted
    on leads (created_at desc)
    where deleted_at is null;
