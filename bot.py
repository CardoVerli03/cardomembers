"""
Telegram Channel Membership Bot
=================================
Manages access to a private Telegram channel with CPA verification.

Features:
1. Auto-grant 24h free trial when user joins channel
2. Welcome message in channel (auto-deletes after 30 sec)
3. /start command with instructions + verify button
4. Golden Goose postback webhook (grants 7 days)
5. 2-hour expiry reminder before kick
6. Hourly scanner to kick expired members
7. /grant <user_id> <days> - admin only
8. /revoke <user_id> - admin only
9. Postback security (basic)
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Supabase
from supabase import create_client, Client

# Aiogram (Telegram Bot)
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery, Update
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# Scheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# FastAPI (Web Server)
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

import uvicorn


# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://your-website.com")
BOT_USERNAME = os.getenv("BOT_USERNAME", "cardomembers")

# Timing settings
FREE_TRIAL_HOURS = 24
REJOIN_TRIAL_MINUTES = 30
CPA_DAYS = 7
REMINDER_HOURS_BEFORE = 2

# Webhook paths
WEBHOOK_PATH = "/webhook/telegram"
POSTBACK_PATH = "/webhook/postback"
HEALTH_PATH = "/health"


# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================
# SUPABASE CLIENT
# ============================================================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# BOT + DISPATCHER
# ============================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ============================================================
# SCHEDULER
# ============================================================
scheduler = AsyncIOScheduler(timezone=timezone.utc)


# ============================================================
# LIFESPAN (startup/shutdown)
# ============================================================
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Manage app startup and shutdown."""
    # --- STARTUP ---
    scheduler.add_job(scan_and_kick_expired, 'interval', hours=1, id='scan_expired')
    scheduler.add_job(send_expiry_reminders, 'interval', hours=1, id='send_reminders', misfire_grace_time=300)
    scheduler.start()
    logger.info("✅ Scheduler started (hourly tasks)")

    base_url = os.getenv("WEBHOOK_BASE_URL")
    if not base_url:
        render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if render_host:
            base_url = f"https://{render_host}"
        else:
            base_url = "http://localhost:8000"

    webhook_url = f"{base_url}{WEBHOOK_PATH}"

    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query", "chat_member"]
    )
    logger.info(f"✅ Telegram webhook set: {webhook_url}")
    logger.info(f"✅ Postback URL: {base_url}{POSTBACK_PATH}?p1={{user_id}}&event=subs")

    yield  # App is now running

    # --- SHUTDOWN ---
    scheduler.shutdown()
    await bot.session.close()
    logger.info("Shutdown complete")


# ============================================================
# FASTAPI APP
# ============================================================
app = FastAPI(title="Telegram Membership Bot", lifespan=lifespan)


# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def get_user(user_id: int) -> dict | None:
    """Get a user from the database by Telegram ID."""
    try:
        result = supabase.table("users").select("*").eq("user_id", user_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"DB error getting user {user_id}: {e}")
        return None


def create_user(user_id: int, username: str | None, hours: float) -> bool:
    """Create a brand new user with trial access."""
    try:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=hours)
        supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "status": "active",
            "expires_at": expires.isoformat(),
            "upgraded": False,
            "reminded": False,
        }).execute()
        logger.info(f"NEW user {user_id} (@{username}) created - {hours}h trial")
        return True
    except Exception as e:
        logger.error(f"DB error creating user {user_id}: {e}")
        return False


def create_paid_user(user_id: int, username: str | None, days: int) -> bool:
    """Create a user with paid/CPA access directly (one atomic operation)."""
    try:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=days)
        supabase.table("users").upsert({
            "user_id": user_id,
            "username": username,
            "status": "active",
            "expires_at": expires.isoformat(),
            "upgraded": True,
            "reminded": False,
        }, on_conflict="user_id").execute()
        logger.info(f"Paid user {user_id} created/granted - {days}d access")
        return True
    except Exception as e:
        logger.error(f"DB error creating paid user {user_id}: {e}")
        return False


def grant_trial(user_id: int, hours: float) -> bool:
    """Grant trial access to an existing user (resets upgraded flag)."""
    try:
        now = datetime.now(timezone.utc)
        new_expires = now + timedelta(hours=hours)
        supabase.table("users").update({
            "status": "active",
            "expires_at": new_expires.isoformat(),
            "upgraded": False,
            "reminded": False,
        }).eq("user_id", user_id).execute()
        logger.info(f"Trial granted to user {user_id} - {hours}h")
        return True
    except Exception as e:
        logger.error(f"DB error granting trial to {user_id}: {e}")
        return False


def grant_paid(user_id: int, days: int) -> bool:
    """Grant paid/CPA access to an existing user."""
    try:
        now = datetime.now(timezone.utc)
        new_expires = now + timedelta(days=days)
        supabase.table("users").update({
            "status": "active",
            "expires_at": new_expires.isoformat(),
            "upgraded": True,
            "reminded": False,
        }).eq("user_id", user_id).execute()
        logger.info(f"Paid access granted to user {user_id} - {days}d")
        return True
    except Exception as e:
        logger.error(f"DB error granting paid to {user_id}: {e}")
        return False


def expire_user(user_id: int) -> bool:
    """Mark user as expired in database."""
    try:
        supabase.table("users").update({
            "status": "expired",
        }).eq("user_id", user_id).execute()
        logger.info(f"User {user_id} marked as expired")
        return True
    except Exception as e:
        logger.error(f"DB error expiring user {user_id}: {e}")
        return False


def get_expired_users() -> list[dict]:
    """Get all active users whose access has already expired."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            supabase.table("users")
            .select("*")
            .eq("status", "active")
            .lt("expires_at", now)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"DB error fetching expired users: {e}")
        return []


def get_reminder_candidates() -> list[dict]:
    """Get users expiring within 2 hours who haven't been reminded yet."""
    try:
        now = datetime.now(timezone.utc)
        threshold = now + timedelta(hours=REMINDER_HOURS_BEFORE)
        result = (
            supabase.table("users")
            .select("*")
            .eq("status", "active")
            .eq("reminded", False)
            .lt("expires_at", threshold.isoformat())
            .gt("expires_at", now.isoformat())
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"DB error fetching reminder candidates: {e}")
        return []


def mark_reminded(user_id: int):
    """Mark a user as reminded (so we don't spam them)."""
    try:
        supabase.table("users").update({"reminded": True}).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"DB error marking reminded for {user_id}: {e}")


def update_username(user_id: int, username: str | None):
    """Update a user's Telegram username."""
    try:
        supabase.table("users").update({"username": username}).eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"DB error updating username for {user_id}: {e}")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def format_time_left(expires_at_str: str) -> str:
    """Format remaining time as a human-readable string."""
    try:
        expires = datetime.fromisoformat(expires_at_str)
        now = datetime.now(timezone.utc)
        diff = expires - now

        if diff.total_seconds() <= 0:
            return "Expired"

        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60

        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except Exception:
        return "Unknown"


async def kick_user(user_id: int):
    """Kick a user from the channel using ban + unban trick."""
    try:
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        await asyncio.sleep(0.3)
        await bot.unban_chat_member(CHANNEL_ID, user_id)
        logger.info(f"Kicked user {user_id} from channel")
    except TelegramForbiddenError:
        logger.warning(f"No permission to kick user {user_id}")
    except TelegramBadRequest as e:
        logger.error(f"Error kicking user {user_id}: {e}")


async def delete_message_later(chat_id: int, message_id: int, seconds: int):
    """Delete a message after a delay. Runs as background task."""
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
        logger.debug(f"Deleted message {message_id} from chat {chat_id}")
    except Exception:
        pass  # Already deleted or no permission


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    """Handle /start - show instructions and user info."""
    user_id = message.from_user.id
    username = message.from_user.username

    # 1. Check if user is in the channel
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        if member.status not in ("member", "administrator", "creator"):
            await message.answer(
                "You must join the channel first to use this bot.\n"
                "Join the channel, then come back here."
            )
            return
    except TelegramBadRequest:
        await message.answer("Error checking channel membership. Try again later.")
        return

    # 2. Check/create user in database
    user = get_user(user_id)
    if not user:
        # Shouldn't happen (join handler creates users), but handle it
        create_user(user_id, username, FREE_TRIAL_HOURS)
        user = get_user(user_id)

    # Update username if it changed
    if user and user.get("username") != username:
        update_username(user_id, username)

    # 3. Build the response message
    time_left = format_time_left(user["expires_at"])
    is_upgraded = user.get("upgraded", False)

    if is_upgraded:
        status_emoji = "✅ Upgraded"
    else:
        status_emoji = "⏳ Free Trial"

    response = (
        f"📋 HOW TO GET FULL ACCESS:\n\n"
        f"1️⃣ Your Telegram ID:\n"
        f"   {user_id}\n\n"
        f"2️⃣ Copy this link and open in your browser:\n"
        f"   {WEBSITE_URL}\n\n"
        f"3️⃣ Enter your Telegram ID on the website\n"
        f"4️⃣ Complete the task to get 7 days access!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {status_emoji}\n"
        f"Time remaining: {time_left}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    # 4. Add verify button
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ I Completed The Task",
            callback_data="verify_cpa"
        )]
    ])

    await message.answer(response, reply_markup=keyboard)


@router.callback_query(F.data == "verify_cpa")
async def verify_cpa(callback: CallbackQuery):
    """Handle the 'I Completed The Task' button click."""
    user_id = callback.from_user.id

    # Check user exists
    user = get_user(user_id)
    if not user:
        await callback.answer(
            "You must join the channel first!",
            show_alert=True
        )
        return

    # Check if already upgraded
    if user.get("upgraded") and user.get("status") == "active":
        time_left = format_time_left(user["expires_at"])
        await callback.answer(
            f"✅ You're already upgraded!\nAccess expires in: {time_left}",
            show_alert=True
        )
        return

    # Not upgraded yet
    await callback.answer(
        "⏳ Confirmation not received yet.\n\n"
        "Complete the task first, then try again in 5 minutes.\n\n"
        "If still not working, contact admin with a screenshot of completion.",
        show_alert=True
    )


# ---- ADMIN COMMANDS ----

@router.message(F.text.startswith("/grant"))
async def cmd_grant(message: Message):
    """Admin: Grant access. Usage: /grant <user_id> <days>"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer("Usage: /grant <user_id> <days>\nExample: /grant 123456789 30")
        return

    try:
        target_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.answer("Invalid format. Use numbers only.\nExample: /grant 123456789 30")
        return

    if days <= 0:
        await message.answer("Days must be a positive number.")
        return

    # Check if user exists
    user = get_user(target_id)
    if not user:
        create_paid_user(target_id, None, days)
    else:
        grant_paid(target_id, days)

    # Try to notify the user
    notification = ""
    try:
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        await bot.send_message(
            target_id,
            f"🎉 Your access has been upgraded to {days} days!\n"
            f"Expires: {expires:%Y-%m-%d %H:%M} UTC"
        )
        notification = "\n✅ User notified."
    except (TelegramForbiddenError, TelegramBadRequest):
        notification = "\n⚠️ Could not notify user (they haven't started the bot yet)."

    await message.answer(f"✅ Granted {days} days to user {target_id}.{notification}")


@router.message(F.text.startswith("/revoke"))
async def cmd_revoke(message: Message):
    """Admin: Revoke access and kick user. Usage: /revoke <user_id>"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("Usage: /revoke <user_id>\nExample: /revoke 123456789")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Invalid user ID. Use a number.")
        return

    # Expire in DB and kick from channel
    expire_user(target_id)
    await kick_user(target_id)

    await message.answer(f"✅ Revoked access and kicked user {target_id}.")


# ---- ADMIN: /stats COMMAND ----

@router.message(F.text == "/stats")
async def cmd_stats(message: Message):
    """Admin: Show membership statistics."""
    if message.from_user.id != ADMIN_ID:
        return

    try:
        # Total users
        total = supabase.table("users").select("user_id", count="exact").execute()
        total_count = total.count if total.count else 0

        # Active users
        active = supabase.table("users").select("user_id", count="exact").eq("status", "active").execute()
        active_count = active.count if active.count else 0

        # Expired users
        expired = supabase.table("users").select("user_id", count="exact").eq("status", "expired").execute()
        expired_count = expired.count if expired.count else 0

        # Upgraded users
        upgraded = supabase.table("users").select("user_id", count="exact").eq("upgraded", True).execute()
        upgraded_count = upgraded.count if upgraded.count else 0

        response = (
            f"📊 CHANNEL STATS\n\n"
            f"👥 Total users: {total_count}\n"
            f"✅ Active: {active_count}\n"
            f"❌ Expired: {expired_count}\n"
            f"💎 Upgraded (CPA): {upgraded_count}\n"
            f"🆓 Free trial: {active_count - upgraded_count}"
        )
        await message.answer(response)
    except Exception as e:
        await message.answer(f"Error fetching stats: {e}")
        logger.error(f"Stats error: {e}")


# ---- CHANNEL JOIN DETECTOR ----

@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Detect when a user joins or leaves the channel."""
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    # Only handle: user JOINED (was left/banned, now member)
    if new_status == "member" and old_status in ("left", "banned"):
        user = event.new_chat_member.user
        user_id = user.id
        username = user.username

        logger.info(f"JOIN: User {user_id} (@{username}) joined channel")

        # Send welcome message in channel (auto-delete after 30 sec)
        welcome_text = (
            f"🎉 Welcome! You have **24h** free access.\n"
            f"After that, upgrade via @{BOT_USERNAME} 👆"
        )
        try:
            welcome_msg = await bot.send_message(
                chat_id=CHANNEL_ID,
                text=welcome_text,
                parse_mode=ParseMode.MARKDOWN
            )
            # Delete after 30 seconds (background task)
            asyncio.create_task(
                delete_message_later(CHANNEL_ID, welcome_msg.message_id, 30)
            )
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")

        # Record user in database
        existing = get_user(user_id)

        if existing and existing.get("upgraded") and existing["status"] == "active":
            # Already upgraded - don't touch their access
            logger.info(f"User {user_id} already upgraded, skipping trial")
            return

        if existing:
            # Returning user (expired) - give 30 min only
            grant_trial(user_id, REJOIN_TRIAL_MINUTES / 60)
            logger.info(f"Returning user {user_id} granted {REJOIN_TRIAL_MINUTES} min trial")
        else:
            # Brand new user - give 24h free trial
            create_user(user_id, username, FREE_TRIAL_HOURS)
            logger.info(f"New user {user_id} granted {FREE_TRIAL_HOURS}h trial")


# ============================================================
# SCHEDULER TASKS (run every 1 hour)
# ============================================================

async def scan_and_kick_expired():
    """Find expired users, mark them expired, and kick from channel."""
    logger.info("=== Running hourly expiry scan ===")
    expired_users = get_expired_users()

    kicked_count = 0
    for user in expired_users:
        user_id = user["user_id"]
        expire_user(user_id)
        await kick_user(user_id)
        kicked_count += 1

    if kicked_count > 0:
        logger.info(f"Kicked {kicked_count} expired users")
    else:
        logger.info("No expired users to kick")


async def send_expiry_reminders():
    """Send 2-hour warning to users about to expire."""
    logger.info("=== Running hourly reminder scan ===")
    candidates = get_reminder_candidates()

    reminded_count = 0
    for user in candidates:
        user_id = user["user_id"]
        time_left = format_time_left(user["expires_at"])

        try:
            await bot.send_message(
                user_id,
                f"⏰ Your channel access expires in {time_left}!\n\n"
                f"DM @{BOT_USERNAME} and follow the instructions to renew."
            )
            mark_reminded(user_id)
            reminded_count += 1
            logger.info(f"Reminder sent to user {user_id}")
        except (TelegramForbiddenError, TelegramBadRequest):
            # User hasn't started the bot - can't message them
            mark_reminded(user_id)
            logger.debug(f"Skipped reminder for {user_id} (bot not started)")

    if reminded_count > 0:
        logger.info(f"Sent {reminded_count} expiry reminders")
    else:
        logger.info("No reminders needed")


# ============================================================
# FASTAPI ENDPOINTS
# ============================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Receive Telegram updates (messages, callbacks, chat_member events)."""
    try:
        update_data = await request.json()
        telegram_update = Update(**update_data)
        await dp.feed_update(bot, telegram_update)
    except Exception as e:
        logger.error(f"Error processing Telegram update: {e}")

    return PlainTextResponse("OK")


@app.get(POSTBACK_PATH)
@app.post(POSTBACK_PATH)
async def golden_goose_postback(request: Request):
    """
    Receive Golden Goose postbacks.
    URL format: /webhook/postback?p1={telegram_user_id}&event=subs
    """
    params = dict(request.query_params)
    user_id_str = params.get("p1")
    event = params.get("event", "")

    # Validate p1 (user ID)
    if not user_id_str:
        logger.warning("Postback received without p1 parameter")
        return PlainTextResponse("Missing p1", status_code=400)

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.warning(f"Invalid p1 value: {user_id_str}")
        return PlainTextResponse("Invalid p1", status_code=400)

    # Only process subscription events
    if event not in ("subs", "redeem", "sale", "lead"):
        logger.info(f"Ignoring postback event '{event}' for user {user_id}")
        return PlainTextResponse("Event ignored")

    logger.info(f"📦 POSTBACK: user={user_id}, event={event}")

    # Update or create user with CPA access
    existing = get_user(user_id)
    if existing:
        grant_paid(user_id, CPA_DAYS)
    else:
        create_paid_user(user_id, None, CPA_DAYS)

    # Try to notify the user
    try:
        expires = datetime.now(timezone.utc) + timedelta(days=CPA_DAYS)
        await bot.send_message(
            user_id,
            f"✅ CPA Verified!\n\n"
            f"You now have 7 days of full access!\n"
            f"Expires: {expires:%Y-%m-%d %H:%M} UTC\n\n"
            f"Enjoy the content! 🎉"
        )
        logger.info(f"Notified user {user_id} of CPA verification")
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.info(f"Could not notify user {user_id} (bot not started by user)")

    return PlainTextResponse("OK")


@app.get(HEALTH_PATH)
async def health_check():
    """Health check endpoint - ping this to keep Render awake."""
    return PlainTextResponse("OK")


# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting bot on port {port}...")
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
