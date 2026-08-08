# Admin Dashboard Guide

## Overview
The admin dashboard allows you to manage groups, drivers, and their assignments through a simple web interface.

## Setup

1. **Install dependencies** (if not already installed):
```bash
pip install flask flask-cors
```

2. **Run the dashboard**:
```bash
python admin_dashboard.py
```

3. **Access the dashboard**:
   - Open your browser and go to: `http://localhost:5000`
   - Or set `ADMIN_PORT` environment variable to use a different port

## Features

### 1. Add Groups
- **Group Name**: A friendly name for the group (e.g., "Group A", "NYC Team")
- **Group Telegram ID**: The Telegram chat ID for the group channel
- **Supervisory Telegram ID**: The Telegram user ID of the supervisor for this group

### 2. Add Drivers
- **Driver Name**: Full name of the driver
- **Driver Telegram ID**: The Telegram user ID of the driver
- **Phone Number**: (Optional) Driver's contact number

### 3. Assign Drivers to Groups
- Select a group from the dropdown
- Select a driver from the dropdown
- Click "Assign Driver" to link them

### 4. Manage Status
- Toggle groups/drivers active/inactive
- Inactive items won't receive new lead requests

## How It Works

1. **When a lead is created**:
   - The bot automatically selects the first active group
   - All active drivers in that group receive a lead request with Accept/Decline buttons
   - The first driver to click "Accept" gets the lead
   - Other drivers see "Request Already Taken"

2. **Group Selection**:
   - Currently uses the first active group
   - Future enhancement: Allow users to select a group when creating a lead

3. **Driver Assignment**:
   - When a driver accepts, they receive full lead details
   - They can then submit receipts using the reference ID

## Database Schema

Make sure you've run the migration SQL:
```sql
-- Run database/schema_multi_group.sql in your Supabase SQL Editor
```

This creates:
- `groups` table
- `drivers` table
- `group_drivers` table (assignments)
- `lead_assignments` table (tracks which driver accepted which lead)

## Troubleshooting

- **Dashboard won't start**: Check that Flask is installed and port 5000 is available
- **Can't add groups/drivers**: Verify Supabase connection and that tables exist
- **Drivers not receiving requests**: Check that:
  - Group is active
  - Driver is active
  - Driver is assigned to the group
  - Telegram IDs are correct


