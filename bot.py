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

    # Admin gets the admin panel directly
    if user_id == ADMIN_ID:
        await cmd_admin(message)
        return

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


# ---- ADMIN STATE TRACKING ----
admin_state = {}  # tracks admin input flow: {admin_id: {"step": "waiting_id"|"waiting_days", "target_id": ...}}

# ---- ADMIN: BUTTON MENU ----

@router.message(F.text == "/admin")
async def cmd_admin(message: Message):
    """Admin: Show admin control panel with buttons."""
    if message.from_user.id != ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="adm_stats"),
            InlineKeyboardButton(text="✅ Grant Access", callback_data="adm_grant_start"),
        ],
        [
            InlineKeyboardButton(text="❌ Revoke & Kick", callback_data="adm_revoke_start"),
        ],
    ])

    await message.answer(
        "⚙️ **ADMIN PANEL**\n\n"
        "What do you want to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )


# ---- ADMIN CALLBACKS ----

@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    """Admin: Show stats via button."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not authorized.", show_alert=True)
        return

    try:
        total = supabase.table("users").select("user_id", count="exact").execute()
        total_count = total.count if total.count else 0

        active = supabase.table("users").select("user_id", count="exact").eq("status", "active").execute()
        active_count = active.count if active.count else 0

        expired = supabase.table("users").select("user_id", count="exact").eq("status", "expired").execute()
        expired_count = expired.count if expired.count else 0

        upgraded = supabase.table("users").select("user_id", count="exact").eq("upgraded", True).execute()
        upgraded_count = upgraded.count if upgraded.count else 0

        response = (
            f"📊 **CHANNEL STATS**\n\n"
            f"👥 Total users: {total_count}\n"
            f"✅ Active: {active_count}\n"
            f"❌ Expired: {expired_count}\n"
            f"💎 Upgraded (CPA): {upgraded_count}\n"
            f"🆓 Free trial: {active_count - upgraded_count}"
        )
        await callback.message.edit_text(response, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await callback.answer(f"Error: {e}", show_alert=True)
        logger.error(f"Stats error: {e}")


@router.callback_query(F.data == "adm_grant_start")
async def adm_grant_start(callback: CallbackQuery):
    """Admin: Start grant flow - ask for user ID."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not authorized.", show_alert=True)
        return

    admin_state[callback.from_user.id] = {"step": "waiting_id", "action": "grant"}
    await callback.message.edit_text(
        "✅ **GRANT ACCESS**\n\n"
        "Send me the **user ID**:\n\n"
        "_Example: 123456789_",
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data == "adm_revoke_start")
async def adm_revoke_start(callback: CallbackQuery):
    """Admin: Start revoke flow - ask for user ID."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not authorized.", show_alert=True)
        return

    admin_state[callback.from_user.id] = {"step": "waiting_id", "action": "revoke"}
    await callback.message.edit_text(
        "❌ **REVOKE & KICK**\n\n"
        "Send me the **user ID**:\n\n"
        "_Example: 123456789_",
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data == "adm_cancel")
async def adm_cancel(callback: CallbackQuery):
    """Admin: Cancel current action."""
    if callback.from_user.id in admin_state:
        del admin_state[callback.from_user.id]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="adm_stats"),
            InlineKeyboardButton(text="✅ Grant Access", callback_data="adm_grant_start"),
        ],
        [
            InlineKeyboardButton(text="❌ Revoke & Kick", callback_data="adm_revoke_start"),
        ],
    ])

    await callback.message.edit_text(
        "⚙️ **ADMIN PANEL**\n\n"
        "What do you want to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )


# ---- ADMIN: HANDLE NUMBER INPUT (for grant/revoke flows) ----

@router.message(F.text)
async def handle_admin_input(message: Message):
    """Handle admin's number inputs during grant/revoke flows."""
    user_id = message.from_user.id

    # Only process if admin is in a flow
    if user_id != ADMIN_ID or user_id not in admin_state:
        return

    state = admin_state[user_id]
    text = message.text.strip()

    if state["step"] == "waiting_id":
        # Expecting user ID
        try:
            target_id = int(text)
        except ValueError:
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm_cancel")]
            ])
            await message.answer(
                "❌ That's not a valid number. Send the user ID or cancel.",
                reply_markup=cancel_kb
            )
            return

        if state["action"] == "grant":
            state["step"] = "waiting_days"
            state["target_id"] = target_id
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm_cancel")]
            ])
            await message.answer(
                f"👤 User: `{target_id}`\n\n"
                f"How many **days** of access?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_kb
            )
        elif state["action"] == "revoke":
            # Do the revoke immediately
            del admin_state[user_id]
            expire_user(target_id)
            await kick_user(target_id)

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="📊 Stats", callback_data="adm_stats"),
                    InlineKeyboardButton(text="✅ Grant Access", callback_data="adm_grant_start"),
                ],
                [
                    InlineKeyboardButton(text="❌ Revoke & Kick", callback_data="adm_revoke_start"),
                ],
            ])
            await message.answer(
                f"✅ User `{target_id}` revoked and kicked from channel.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )

    elif state["step"] == "waiting_days":
        # Expecting days
        try:
            days = int(text)
        except ValueError:
            cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm_cancel")]
            ])
            await message.answer(
                "❌ That's not a valid number. Send the number of days or cancel.",
                reply_markup=cancel_kb
            )
            return

        if days <= 0:
            await message.answer("❌ Must be at least 1 day.")
            return

        target_id = state["target_id"]
        del admin_state[user_id]

        # Grant access
        user = get_user(target_id)
        if not user:
            create_paid_user(target_id, None, days)
        else:
            grant_paid(target_id, days)

        # Try to notify user
        notification = ""
        try:
            expires = datetime.now(timezone.utc) + timedelta(days=days)
            await bot.send_message(
                target_id,
                f"🎉 Your access has been upgraded to {days} days!\n"
                f"Expires: {expires:%Y-%m-%d %H:%M} UTC"
            )
            notification = "\n📩 User notified."
        except (TelegramForbiddenError, TelegramBadRequest):
            notification = "\n⚠️ User hasn't started the bot (can't notify)."

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Stats", callback_data="adm_stats"),
                InlineKeyboardButton(text="✅ Grant Access", callback_data="adm_grant_start"),
            ],
            [
                InlineKeyboardButton(text="❌ Revoke & Kick", callback_data="adm_revoke_start"),
            ],
        ])

        await message.answer(
            f"✅ Granted **{days} days** to user `{target_id}`.{notification}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )


# ---- FALLBACK: catch /start for admin too ----

@router.message(F.text == "/admin_start")
async def cmd_admin_start(message: Message):
    """Admin: alias - show admin panel."""
    if message.from_user.id != ADMIN_ID:
        return
    await cmd_admin(message)


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

        # ADMIN IS PERMANENT - skip everything
        if user_id == ADMIN_ID:
            logger.info(f"JOIN: Admin {user_id} joined - permanent access, no trial")
            return

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

        if not existing:
            # Brand new user - give 24h free trial
            create_user(user_id, username, FREE_TRIAL_HOURS)
            logger.info(f"New user {user_id} granted {FREE_TRIAL_HOURS}h trial")
            return

        # User exists in DB - check if they still have active time
        if existing["status"] == "active" and existing.get("expires_at"):
            expires = datetime.fromisoformat(existing["expires_at"])
            if expires > datetime.now(timezone.utc):
                # Still has time remaining - don't touch anything
                logger.info(f"User {user_id} rejoined with active access, no change")
                return

        # User was expired or time ran out - give 30 min only
        grant_trial(user_id, REJOIN_TRIAL_MINUTES / 60)
        logger.info(f"Expired user {user_id} rejoined, granted {REJOIN_TRIAL_MINUTES} min trial")


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
        # Never kick the admin
        if user_id == ADMIN_ID:
            continue
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
