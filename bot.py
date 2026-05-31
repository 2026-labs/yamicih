import os
import random
import logging
import asyncio
from itertools import cycle
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
import google.generativeai as genai
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIG — semua dibaca dari environment variables
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Baca semua Gemini keys dari env: GEMINI_KEY_1, GEMINI_KEY_2, dst
GEMINI_KEYS = [
    os.environ[f"GEMINI_KEY_{i}"]
    for i in range(1, 20)
    if os.environ.get(f"GEMINI_KEY_{i}")
]

# Baca semua Groq keys dari env: GROQ_KEY_1, GROQ_KEY_2, dst (opsional)
GROQ_KEYS = [
    os.environ[f"GROQ_KEY_{i}"]
    for i in range(1, 10)
    if os.environ.get(f"GROQ_KEY_{i}")
]

# ID Telegram lo — buat nerima notif /start. Cara cari: chat ke @userinfobot
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# Probabilitas bot bales pesan di GRUP (0.0 - 1.0). 0.4 = 40% chance
REPLY_CHANCE = 0.9

# Di DM privat bot SELALU bales (bisa diubah ke REPLY_CHANCE juga kalau mau)
DM_ALWAYS_REPLY = True

# Persona bot
SYSTEM_PROMPT = """You are an active member of a crypto community. Casual, opinionated, and always engaged.
Keep replies short — 1 to 3 sentences max. No essays.
Sometimes skeptical, sometimes bullish, always sounding like a real person.
Match the language of whoever you're talking to.
Never reveal you are an AI or a bot."""

# Kata kunci yang WAJIB ditrigger di grup (probabilitas jadi 100%)
TRIGGER_KEYWORDS = [
    "btc", "bitcoin", "eth", "ethereum", "pump", "dump", "bull", "bear", "early",
    "altcoin", "solana", "sol", "bnb", "crypto", "kripto", "kapan", "prediksi",
    "scam", "rug", "hodl", "dca", "staking", "airdrop", "nft", "defi", "cuan"
]

# ============================================================
# KEY ROTATOR
# ============================================================

class KeyRotator:
    def __init__(self, keys, name="API"):
        self.keys = keys
        self.name = name
        self._cycle = cycle(keys) if keys else iter([])
        self._exhausted = set()

    def next_key(self):
        if not self.keys:
            return None
        for _ in range(len(self.keys)):
            key = next(self._cycle)
            if key not in self._exhausted:
                return key
        return None

    def mark_exhausted(self, key):
        self._exhausted.add(key)
        logger.warning(f"[{self.name}] Key ...{key[-6:]} kena rate limit ({len(self._exhausted)}/{len(self.keys)} exhausted)")

    def reset(self):
        self._exhausted.clear()
        logger.info(f"[{self.name}] Key rotation reset")

    @property
    def has_available(self):
        return bool(self.keys) and len(self._exhausted) < len(self.keys)


gemini_rotator = KeyRotator(GEMINI_KEYS, "Gemini")
groq_rotator = KeyRotator(GROQ_KEYS, "Groq")

# ============================================================
# AI CALLER
# ============================================================

def call_gemini(prompt: str) -> str | None:
    for attempt in range(len(GEMINI_KEYS)):
        key = gemini_rotator.next_key()
        if not key:
            logger.error("Semua Gemini keys exhausted!")
            return None
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                system_instruction=SYSTEM_PROMPT
            )
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            err = str(e).lower()
            if "quota" in err or "rate" in err or "429" in err or "exhausted" in err:
                gemini_rotator.mark_exhausted(key)
                logger.info(f"Gemini rate limit, coba key lain... (attempt {attempt+1})")
                continue
            else:
                logger.error(f"Gemini error: {e}")
                return None
    return None


def call_groq(prompt: str) -> str | None:
    for attempt in range(len(GROQ_KEYS)):
        key = groq_rotator.next_key()
        if not key:
            return None
        try:
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 200,
                "temperature": 0.8
            }
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers, timeout=10
            )
            if resp.status_code == 429:
                groq_rotator.mark_exhausted(key)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"Groq error: {e}")
            return None
    return None


def get_ai_response(prompt: str) -> str | None:
    if gemini_rotator.has_available:
        result = call_gemini(prompt)
        if result:
            return result
    if groq_rotator.has_available:
        logger.info("Fallback ke Groq...")
        return call_groq(prompt)
    return None


# ============================================================
# HANDLERS
# ============================================================

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /start — bales user + kirim notif ke owner"""
    user = update.effective_user
    chat = update.effective_chat

    # Bales user yang /start
    await update.message.reply_text(
        "Halo! Tanya aja soal crypto, gw siap diskusi 📈"
    )

    # Kirim notif ke owner kalau OWNER_ID sudah diset
    if OWNER_ID:
        notif = (
            f"🔔 Ada yang /start bot!\n\n"
            f"👤 Nama: {user.full_name}\n"
            f"🆔 User ID: <code>{user.id}</code>\n"
            f"📛 Username: @{user.username or '-'}\n"
            f"💬 Chat type: {chat.type}"
        )
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=notif,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Gagal kirim notif ke owner: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler pesan biasa — grup & DM"""
    message = update.message
    if not message or not message.text:
        return

    text = message.text.lower()
    chat_type = message.chat.type
    is_private = chat_type == "private"
    is_group = chat_type in ["group", "supergroup"]

    # Tentukan apakah bot harus bales
    if is_private:
        should_reply = DM_ALWAYS_REPLY
    elif is_group:
        has_keyword = any(kw in text for kw in TRIGGER_KEYWORDS)
        should_reply = has_keyword or (random.random() < REPLY_CHANCE)
    else:
        return

    if not should_reply:
        return

    # Delay natural (di DM lebih cepet, di grup lebih santai)
    delay = random.uniform(0.5, 2) if is_private else random.uniform(1, 4)
    await asyncio.sleep(delay)

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    response = get_ai_response(message.text)

    if response:
        await message.reply_text(response)
        logger.info(f"[{'DM' if is_private else 'Grup'}] Replied: {message.text[:50]}...")
    else:
        logger.warning("Semua AI provider exhausted, skip reply")


# ============================================================
# RESET KEY ROTATION TIAP JAM
# ============================================================

async def reset_keys_job(context: ContextTypes.DEFAULT_TYPE):
    gemini_rotator.reset()
    groq_rotator.reset()


# ============================================================
# MAIN
# ============================================================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(reset_keys_job, interval=3600, first=3600)

    logger.info("Bot nyala!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
