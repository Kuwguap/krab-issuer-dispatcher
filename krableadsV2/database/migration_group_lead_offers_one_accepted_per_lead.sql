-- Only one group may have status = 'accepted' per lead (prevents concurrent double-accept).
-- Run in Supabase SQL editor after fixing any existing duplicates:
--   SELECT lead_id, COUNT(*) FROM group_lead_offers WHERE status = 'accepted' GROUP BY lead_id HAVING COUNT(*) > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_group_lead_offers_one_accepted_per_lead
ON group_lead_offers (lead_id)
WHERE status = 'accepted';
