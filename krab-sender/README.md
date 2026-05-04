# 🦀 Krab Dispatch Bot

A high-efficiency Telegram bot that bridges the gap between messaging and professional email delivery, featuring a serverless admin dashboard for real-time tracking.

## 🚀 Overview
Krab Dispatch allows users to forward documents (PDFs) via Telegram, collects specific client metadata, and dispatches them to a designated email address. All activities are logged and visualized in a secure web-based admin panel.

### Key Features
* **Telegram-to-Email Pipeline**: Automated forwarding with custom formatting.
* **Data Enrichment**: Captures Telegram handles, timestamps, and custom client details.
* **Serverless Admin Panel**: A password-protected dashboard to monitor bot health and logs.
* **Automated Summaries**: Weekly performance reports (Saturdays 12 AM NJ Time).

## 🛠 Tech Stack
* **Bot Framework**: `python-telegram-bot` or `GramJS`
* **Backend**: Node.js/Python (Serverless Functions)
* **Database**: MongoDB or Supabase (PostgreSQL)
* **Frontend**: Next.js or React (Hosted on Vercel/Netlify)
* **Email Service**: SendGrid, Mailgun, or AWS SES

## 📬 Email Format
The recipient will receive emails in the following structure:
> **Subject**: CLIENT  
> {client details}  
> **Sent by**: {telegram_name}  
> **Source**: Krab Dispatch by [johnnybravomadeit](https://t.me/johnnybravomadeit)  
> *[Attachment: document.pdf]*

## 🔐 Admin Access
The public-facing dashboard is protected.
* **Default Password**: `AdminPassword123!` (Ensure to change this in environment variables).

vercel: richierodney434
render: richierodney434
supabase:krabsender07
github: richierodney5
