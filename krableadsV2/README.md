# Krab Issuer — Telegram lead intake & Monday.com integration

A robust Python-based middleware (**Krab Issuer**) that automates lead intake, encrypts sensitive contact data, and synchronizes dispatch details between Telegram, Monday.com, and OneTimeSecret.

## 🛠 Tech Stack
- **Language:** Python 3.10+
- **Bot Framework:** `python-telegram-bot` (Asynchronous)
- **Database:** Supabase (PostgreSQL) for state management & lead logging
- **Infrastructure:** Render.com (Web Service)
- **External APIs:** Monday.com GraphQL, OneTimeSecret API

## 🔄 The Workflow
1. **Intake Phase 1:** User provides vehicle and delivery details.
2. **Intake Phase 2:** User provides phone number and price.
3. **Encryption:** The system sends the phone number to OneTimeSecret with the passphrase `DispatchPassword`.
4. **CRM Sync:** Data is pushed to Monday.com with auto-calculated NY-Time stamps and 30-day expirations.
5. **Distribution:** - Full data sent to the **Driver**.
   - Encrypted link + Response 1 sent to the **Telegram Group**.
   - Full log sent to a specific **Supervisory Telegram ID**.

## 🚀 Setup Instructions

### 1. Local Development Setup

1. **Clone the repository** (if applicable) or navigate to the project directory.

2. **Create a virtual environment:**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

4. **Set up Supabase:**
   - Create a Supabase project at https://supabase.com
   - Run the SQL schema from `database/schema.sql` in your Supabase SQL Editor
   - Run the multi-group schema from `database/schema_multi_group.sql` for group/driver support
   - Copy your Supabase URL and API key

5. **Configure environment variables:**
   - Create a `.env` file in the project root
   - Add all required environment variables (see below)

6. **Run the bot:**
```bash
python bot.py
```

### 2. Render.com Deployment

1. **Connect your repository** to Render.com
2. **Create a new Web Service** using the `render.yaml` configuration
3. **Add all environment variables** in the Render dashboard
4. **Deploy** - Render will automatically build and deploy your bot

## ⚙️ Environment Variables Required

To run this project, you will need to add the following environment variables:

### Required Variables:
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token from @BotFather
- `MONDAY_API_KEY` - Your Monday.com API key
- `MONDAY_BOARD_ID` - The ID of your Monday.com board
- `ONETIMESECRET_USERNAME` - Your OneTimeSecret username
- `ONETIMESECRET_API_KEY` - Your OneTimeSecret API key
- `SUPABASE_URL` - Your Supabase project URL
- `SUPABASE_KEY` - Your Supabase API key

### Optional Variables:
- `ADMIN_PORT` - Port for admin dashboard (default: 5000)
- Note: Groups, drivers, and supervisory IDs are now managed through the admin dashboard

## 🎛️ Admin Dashboard

The project includes a web-based admin dashboard for managing groups, drivers, and assignments.

**To start the admin dashboard:**
```bash
python admin_dashboard.py
```

Then open `http://localhost:5000` in your browser.

See [ADMIN_DASHBOARD.md](ADMIN_DASHBOARD.md) for detailed instructions.

## 📝 Notes

- **Multi-Group Support**: The bot now supports multiple groups. Use the admin dashboard to add groups, drivers, and assign drivers to groups. When a lead is created, it's sent to all active drivers in the first active group with Accept/Decline buttons.

- **Driver Assignment**: When a lead is created, all active drivers in the group receive a request with Accept/Decline buttons. The first driver to accept gets the lead; others see "Request Already Taken".

- **Monday.com Integration**: The GraphQL mutation in `utils/monday.py` uses placeholder column IDs. You'll need to update these to match your actual Monday.com board column structure. The code includes detailed comments explaining which field maps to which Monday.com column.

- **Message Distribution**:
  - **Group**: Receives message without user, phone, and price (starts with "NEW CLIENT")
  - **Supervisory**: Receives full detailed message with all information including reference ID
  - **Drivers**: Receive lead request with Accept/Decline buttons. After accepting, they get full details.
  - **Monday.com**: Receives only specific fields (issue date, expiration date, price, phone, username, full tag info)

- **Monday.com Fields Sent**:
  - Issue date (auto, NY-Time)
  - Expiration date (30 days from issue date)
  - Total client paid (price)
  - Status: "Pending" (changes to "Paid" when receipt picture is filled)
  - whos lead it was (telegram username)
  - phone number (plaintext, before encryption)
  - full tag info (Phase 1 response from user)
  - Other fields set to null (will be filled later: receipt picture, driver name, etc.)

- The phone number parsing in `bot.py` is basic - you may want to enhance it with regex validation.
- Make sure your Telegram bot has permission to send messages to the specified chat IDs.