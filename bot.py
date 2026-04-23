"""
🚀 UJANGILI EDITION: Telegram Channel Membership Bot
=====================================================
Built for: Mbeya Tech-Scrapers & Automation Kings 👿
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from supabase import create_client, Client
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager

# ============================================================
# ⚙️ CONFIGURATION & SECRETS
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBSITE_URL = os.getenv("WEBSITE_URL")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone=timezone.utc)

# ============================================================
# 👿 THE "GHOST" HANDLERS (UJANGILI GETINI)
# ============================================================

@router.chat_member()
async def on_chat_member_updated(update: ChatMemberUpdated):
    """Detection logic: Detect new leeches and grant 24h trial."""
    if update.new_chat_member.status == ChatMemberStatus.MEMBER:
        user_id = update.from_user.id
        username = update.from_user.username
        
        # Check if user exists, if not, create trial
        user = get_user(user_id)
        if not user:
            create_user(user_id, username, 24) # 24h trial
            
            # Send warning message that self-destructs in 20 seconds
            warning_text = (
                f"⚠️ **GHOST ACCESS GRANTED** ⚠️\n\n"
                f"Hello @{username or 'User'}, you have been granted **24 HOURS** of free access.\n"
                f"Upgrade to Premium via our bot to avoid being kicked tomorrow!\n\n"
                f"🔥 _This message will self-destruct in 20s_"
            )
            msg = await bot.send_message(CHANNEL_ID, warning_text, parse_mode=ParseMode.MARKDOWN)
            asyncio.create_task(delete_message_later(CHANNEL_ID, msg.message_id, 20))

# ============================================================
# 🛠️ NAVIGATION & UI (WITH BACK BUTTONS)
# ============================================================

def main_menu_kb(is_upgraded, time_left, user_id):
    """The sleek main menu with Ujangili-style UI."""
    status = "💎 PREMIUM" if is_upgraded else "🆓 TRIAL"
    
    keyboard = [
        [InlineKeyboardButton(text="🚀 ACTIVATE 7 DAYS (CPA)", url=f"{WEBSITE_URL}?p1={user_id}")],
        [InlineKeyboardButton(text="💰 PAY CRYPTO (1 MONTH)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🔄 REFRESH STATUS", callback_data="refresh_status")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id) or create_user(user_id, message.from_user.username, 24)
    
    time_left = format_time_left(user["expires_at"])
    
    text = (
        f"👿 **SCALPER DASHBOARD** 👿\n\n"
        f"Your ID: `{user_id}`\n"
        f"Status: **{'PREMIUM' if user.get('upgraded') else 'FREE TRIAL'}**\n"
        f"Access Ends In: `{time_left}`\n\n"
        f"Select an option below to stay in the channel:"
    )
    await message.answer(text, reply_markup=main_menu_kb(user.get("upgraded"), time_left, user_id), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "pay_crypto")
async def pay_crypto(callback: CallbackQuery):
    """Crypto payment menu with a BACK button."""
    text = (
        "💳 **CRYPTO PAYMENT (USDT TRC20)**\n\n"
        "Send **2 USDT** to the address below:\n"
        "`TX7xxxxxxxxxxxxxxxxxxxxxxxxx`\n\n"
        "Once paid, send the **Transaction ID (TXID)** to @Admin for verification."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 BACK TO MENU", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    """Return to start menu logic."""
    await cmd_start(callback.message)
    await callback.message.delete()

# ============================================================
# 🧹 THE "FAGIA" SYSTEM (AUTO-KICK SCANNER)
# ============================================================

async def scan_and_kick_expired():
    """The hourly scanner that kicks users without mercy."""
    expired_users = get_expired_users()
    for user in expired_users:
        user_id = user["user_id"]
        try:
            await kick_user(user_id)
            expire_user(user_id)
            # Notify user
            await bot.send_message(user_id, "❌ **ACCESS EXPIRED**\n\nYou have been kicked from the channel. Subscribe again to get access!")
        except Exception as e:
            logger.error(f"Failed to kick {user_id}: {e}")

# ============================================================
# 💉 DATABASE INJECTIONS (UJANGILI HELPERS)
# ============================================================

def get_user(user_id: int):
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None

def create_user(user_id: int, username: str, hours: int):
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    data = {"user_id": user_id, "username": username, "expires_at": expires.isoformat(), "status": "active"}
    supabase.table("users").upsert(data).execute()
    return data

def get_expired_users():
    now = datetime.now(timezone.utc).isoformat()
    res = supabase.table("users").select("*").eq("status", "active").lt("expires_at", now).execute()
    return res.data or []

# ============================================================
# 🚀 LIFESPAN & WEBHOOKS
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(scan_and_kick_expired, 'interval', minutes=30) # Kick every 30 mins
    scheduler.start()
    await bot.set_webhook(url=f"{os.getenv('WEBHOOK_BASE_URL')}/webhook/telegram")
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/webhook/postback")
async def postback_handler(p1: int, event: str):
    """CPA Signal from Golden Goose."""
    if event in ["subs", "redeem"]:
        grant_paid(p1, 7) # Grant 7 days
        await bot.send_message(p1, "💎 **PRO ACCESS ACTIVATED!**\nYour 7-day CPA access is now live. Enjoy!")
    return "OK"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
