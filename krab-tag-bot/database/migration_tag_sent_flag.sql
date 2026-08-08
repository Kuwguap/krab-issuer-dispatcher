-- Manual leads now send the supervisory notice + NJ temp-tag PDF to the
-- selected group AT CREATION (mirroring the website flow). This flag records
-- that so the group-accept path does not send the tag a second time.
-- Website leads keep using external_order_id for the same suppression; renewals
-- always mint a fresh tag regardless of this flag.
ALTER TABLE public.leads
  ADD COLUMN IF NOT EXISTS tag_sent_at_creation boolean NOT NULL DEFAULT false;
