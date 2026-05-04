# 🗺 Development Roadmap: Krab Dispatch

## Phase 1: Core Bot Development 🤖
- [ ] Initialize Telegram Bot API with BotFather.
- [ ] Implement `Document` listener to trigger logging sequence.
- [ ] State Machine setup:
    1. Receive File -> Log User/Filename/Time.
    2. Prompt for "Client Details".
    3. Wait for text input.
- [ ] Integration with SMTP/Email API to forward the payload.

## Phase 2: Database & Backend 🗄️
- [ ] Schema Design:
    - `Transactions`: ID, Telegram Name, Handle, Filename, Client Details, Timestamp, Delivery Status.
- [ ] Build API endpoints to feed the Admin Dashboard.
- [ ] Implement Saturday 12 AM (NJ Time) CRON job for summary generation.

## Phase 3: Admin Dashboard (Serverless) 💻
- [ ] UI Implementation:
    - **Health Check**: Bot Up/Down status.
    - **Real-time Feed**: Last item processed.
    - **Data Table**: Sortable/Filterable list of all transmissions.
- [ ] Security: Password protection layer for the `/admin` route.
- [ ] "Generate Summary" button for manual PDF/Text reports.

## Phase 4: Testing & Deployment 🚀
- [ ] Test file handling for large PDFs.
- [ ] Verify NJ Timezone accuracy for CRON jobs.
- [ ] Deploy frontend to Vercel and bot logic to a VPS or Serverless provider.

Pro-Tips for your implementation:
Security: Please do not hardcode AdminPassword123! in your actual code. Move it to an .env file immediately.

Timezones: Since you specified NJ Time (Eastern Time), ensure your server uses America/New_York in its logic, especially for the Saturday 12 AM summary, or you'll be off by a few hours depending on where the server is hosted.

# Updated for Rodney - accountant.com configuration
EMAIL_PROVIDER=smtp
EMAIL_FROM_ADDRESS=SendReceiptToday@accountant.com
EMAIL_TO_ADDRESS=richierodney5@gmail.com
EMAIL_SMTP_HOST=smtp.mail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USERNAME=SendReceiptToday@accountant.com
EMAIL_SMTP_PASSWORD=10ThousandPerDay!!!