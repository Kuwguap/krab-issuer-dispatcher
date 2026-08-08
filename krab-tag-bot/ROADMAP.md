# Project Roadmap: Krab Issuer (Telegram lead bot)

## Phase 1: Infrastructure Setup ✅
- [ ] Initialize GitHub Repository.
- [ ] Set up Render.com Web Service environment.
- [ ] Initialize Supabase project and create `leads` and `states` tables.

## Phase 2: Telegram Bot Development 🏗️
- [ ] Implement `/start` command and multi-step conversation handler.
- [ ] **State 1:** Capture and parse vehicle/delivery details.
- [ ] **State 2:** Capture phone/price and trigger OneTimeSecret API.
- [ ] Logic for photo/receipt upload and "Status" trigger.

## Phase 3: API Integrations 🔗
- [ ] **OneTimeSecret:** Implement POST request with `DispatchPassword` passphrase.
- [ ] **Monday.com:** Map fields:
    - Auto-calculate Issue Date (NY Time) & Expiration (+30 days).
    - Map `whos lead` to Telegram username.
    - Setup status transition logic (Pending -> Paid).

## Phase 4: Routing & Dispatch 📡
- [ ] Logic to route messages to specific Group IDs based on User ID.
- [ ] Separate formatting for Driver vs. Group (Plaintext vs. Secret Link).

## Phase 5: Testing & Deployment 🚀
- [ ] End-to-end testing of data flow.
- [ ] Final deployment to Render.com.