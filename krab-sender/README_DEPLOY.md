# 🚀 Deploying Krab Dispatch Bot to Render

This guide will help you deploy the Telegram bot to Render.

## Prerequisites

1. A Render account (sign up at https://render.com)
2. Your Telegram bot token from BotFather
3. Your email/SMTP credentials
4. Database URL (PostgreSQL recommended for production)

## Step 1: Prepare Your Environment Variables

Create a `.env` file locally with all required variables (see `config.example.env.txt`), or prepare to add them in Render's dashboard.

## Step 2: Deploy Using Render Blueprint (Recommended)

### Option A: Using render.yaml (Infrastructure as Code)

1. Push your code to a GitHub repository
2. Go to Render Dashboard → New → Blueprint
3. Connect your GitHub repository
4. Render will detect `render.yaml` and create the services
5. Add your environment variables in the Render dashboard for each service

### Option B: Manual Setup

1. Go to Render Dashboard → New → Background Worker
2. Connect your GitHub repository
3. Configure:
   - **Name**: `krab-dispatch-bot`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python -m bot.main`
4. Add all environment variables (see below)

## Step 3: Required Environment Variables

Add these in Render's Environment Variables section:

### Required Variables:
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
EMAIL_PROVIDER=smtp
EMAIL_FROM_ADDRESS=SendReceiptToday@accountant.com
EMAIL_TO_ADDRESS=richierodney5@gmail.com
EMAIL_SMTP_HOST=smtp.mail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USERNAME=SendReceiptToday@accountant.com
EMAIL_SMTP_PASSWORD=your_password_here
ADMIN_PASSWORD=your_admin_password
DATABASE_URL=your_postgresql_connection_string
API_BASE_URL=https://your-api-service.onrender.com
```

### Database Setup:
- For production, use Render's PostgreSQL database
- Go to Render Dashboard → New → PostgreSQL
- Copy the Internal Database URL and use it as `DATABASE_URL`
- The bot will automatically create tables on first run

## Step 4: Deploy the Backend API (Optional)

If you want to deploy the admin API separately:

1. Go to Render Dashboard → New → Web Service
2. Configure:
   - **Name**: `krab-dispatch-api`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn backend.api:app --host 0.0.0.0 --port $PORT`
3. Add environment variables:
   - `ADMIN_PASSWORD`
   - `DATABASE_URL` (same as bot)
4. Update `API_BASE_URL` in bot's environment to point to this service

## Step 5: Verify Deployment

1. Check the bot logs in Render dashboard
2. Look for: "Starting Krab Dispatch bot..."
3. Send `/start` to your bot on Telegram
4. Verify it responds correctly

## Troubleshooting

### Bot Not Starting
- Check logs for missing environment variables
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Ensure database connection is working

### Email Not Sending
- Verify SMTP credentials are correct
- Check if port 587 is accessible (try 465 if blocked)
- Review email logs in Render dashboard

### Database Connection Issues
- Verify `DATABASE_URL` format: `postgresql://user:pass@host:port/dbname`
- Ensure database is accessible from Render
- Check if tables are created (check logs)

## Notes

- The bot runs as a background worker (always on)
- Render free tier has limitations (spins down after inactivity)
- For production, consider Render's paid plans for 24/7 uptime
- Monitor logs regularly for any issues

## Support

If you encounter issues:
1. Check Render logs
2. Verify all environment variables are set
3. Test locally first with the same `.env` values





