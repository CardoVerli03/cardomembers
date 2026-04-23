# Telegram Channel Membership Bot

A simple but strong Telegram bot that manages access to your private channel via CPA verification.

## Features

1. **Auto 24h free trial** when user joins channel
2. **Welcome message** in channel (auto-deletes after 30 sec)
3. **/start command** with instructions + verify button
4. **Golden Goose postback** webhook (grants 7 days)
5. **2-hour expiry reminder** before kick
6. **Hourly scanner** to kick expired members
7. **/grant** - Admin grant access
8. **/revoke** - Admin kick user
9. **/stats** - Admin view channel stats

## Setup Steps

### Step 1: Supabase Database
1. Go to [supabase.com](https://supabase.com) → your project
2. Click **SQL Editor** → **New Query**
3. Paste the contents of `setup.sql`
4. Click **Run**

### Step 2: Environment Variables
1. Copy `.env.example` to `.env`
2. Fill in your actual values:

| Variable | Where to get it |
|----------|----------------|
| `BOT_TOKEN` | @BotFather → /newbot or /token |
| `ADMIN_ID` | @userinfobot → sends your numeric ID |
| `CHANNEL_ID` | Add @RawDataBot to channel → sends ID like -1001234567890 |
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_KEY` | Supabase → Settings → API → anon public key |
| `WEBSITE_URL` | Your website where users enter their ID |
| `BOT_USERNAME` | Your bot's username from BotFather (without @) |

### Step 3: Deploy to Render
1. Push this project to a GitHub repo (EXCLUDE .env!)
2. Go to [render.com](https://render.com) → **New** → **Web Service**
3. Connect your GitHub repo
4. Set these:
   - **Build Command**: (leave empty - auto detected)
   - **Start Command**: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
   - **Environment**: Python 3
5. Add all environment variables from your `.env`
6. Click **Create Web Service**

### Step 4: Set Up Bot Permissions
1. Add your bot to the channel as **admin**
2. Grant these permissions:
   - **Ban Users** (for kicking expired members)
   - **Post Messages** (for welcome message)

### Step 5: Set Up Golden Goose Postback
1. In Golden Goose, set your postback URL:
   ```
   https://your-app-name.onrender.com/webhook/postback?p1={p1}&event={event}
   ```
   Replace `your-app-name` with your actual Render app name.
2. Set the `{p1}` parameter to the user's Telegram ID.

### Step 6: Keep Render Awake (IMPORTANT!)
Render free tier sleeps after 15 minutes of no traffic. Postbacks might be lost!

**Fix using cron-job.org (FREE):**
1. Go to [cron-job.org](https://cron-job.org)
2. Create a free account
3. Click **Create Cronjob**
4. Set:
   - **Title**: Keep Bot Alive
   - **URL**: `https://your-app-name.onrender.com/health`
   - **Schedule**: Every 5 minutes
5. Save and enable it

This pings your bot every 5 minutes so it never sleeps.

## Admin Commands

| Command | Description |
|---------|-------------|
| `/grant <user_id> <days>` | Grant access to user (e.g., /grant 123456789 30) |
| `/revoke <user_id>` | Kick user and revoke access |
| `/stats` | Show channel membership stats |

## How It Works

```
User joins channel
    ↓
Bot records them + 24h free trial
Bot sends welcome (deletes after 30 sec)
    ↓
User DMs bot → Gets instructions + Telegram ID
    ↓
User goes to website → Enters ID → Completes CPA
    ↓
Golden Goose sends postback → Bot grants 7 days
Bot DMs user: "✅ CPA Verified!"
    ↓
2 hours before expiry → Bot DMs reminder
    ↓
Time runs out → Bot kicks user from channel
    ↓
User can rejoin → Gets 30 min trial only
```

## Troubleshooting

**Bot not responding?**
- Check if Render service is running
- Verify BOT_TOKEN is correct
- Check Render logs for errors

**Welcome message not appearing?**
- Bot must be admin in channel with "Post Messages" permission

**Users not being kicked?**
- Bot must be admin with "Ban Users" permission

**Postbacks not working?**
- Verify postback URL is correct in Golden Goose
- Check Render logs for incoming requests
- Make sure cron-job.org is pinging /health every 5 min

**Users getting kicked immediately?**
- Check Supabase - make sure their expires_at is in the future
- Check server timezone is UTC
