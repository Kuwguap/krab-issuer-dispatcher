# ClientsPhoneNumber (OneTimeSecret-compatible) Roadmap

## Phase 1 (Done / In Progress)
- [x] Create Supabase tables for secrets + admin config
- [x] Implement `POST /api/v1/share` (OneTimeSecret-compatible contract)
- [x] Implement unlock UI at `/secret/[secret_key]`
- [x] Implement unlock endpoint `POST /api/v1/unlock`
- [x] Add admin UI at `/admin` to set:
  - passphrase required to unlock notes
  - optional expiration toggle + duration (days)
  - optional “delete after first unlock” toggle (default OFF)

## Phase 2 (Future hardening)
- [ ] Add admin access protection (basic gate / env password)
- [ ] Add rate limiting to unlock endpoint
- [ ] Add audit logging table (who unlocked, when)

