import os
import re
import json
import base64
import hashlib
import logging
import threading
from datetime import datetime, time as dtime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, CallbackQueryHandler,
    CallbackContext, Filters
)

load_dotenv(override=False)

def env(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env: {name}")
    return val

BOT_TOKEN = env("BOT_TOKEN", required=True)
ADMIN_IDS = {int(x) for x in env("ADMIN_IDS", "").replace(" ", "").split(",") if x}
NOTIFY_CHAT_ID = env("NOTIFY_CHAT_ID")
MIN_PRICE = float(env("MIN_PRICE", "0") or 0)
WORK_START = env("WORK_START", "00:00")
WORK_END = env("WORK_END", "23:59")
KWORK_CHECK_INTERVAL = int(env("KWORK_CHECK_INTERVAL", "300"))
KWORK_LOGIN = env("KWORK_LOGIN", "")
KWORK_PASSWORD = env("KWORK_PASSWORD", "")
PROXY_URL = env("PROXY_URL", "")
DATA_FILE = env("DATA_FILE", "data.json")
LOG_FILE = env("LOG_FILE", "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger("kwork_bot")

def get_fernet():
    key_source = hashlib.sha256(BOT_TOKEN.encode()).digest()
    key = base64.urlsafe_b64encode(key_source)
    return Fernet(key)

FERNET = get_fernet()

def encrypt_text(plain: str) -> str:
    if not plain:
        return ""
    return FERNET.encrypt(plain.encode()).decode()

def decrypt_text(token: str) -> str:
    if not token:
        return ""
    try:
        return FERNET.decrypt(token.encode()).decode()
    except Exception:
        logger.error("Decryption failed")
        return ""

KWORK_PASSWORD_ENC = encrypt_text(KWORK_PASSWORD) if KWORK_PASSWORD else ""

def get_kwork_password():
    return decrypt_text(KWORK_PASSWORD_ENC)

data_lock = threading.Lock()
DEFAULT_DATA = {
    "keywords": [],
    "template": (
        "Здравствуйте! Заинтересовал заказ «{title}» (ключевое слово: {keyword}). "
        "Готов приступить к работе в ближайшее время. Ссылка: {link}"
    ),
    "paused": False,
    "seen_kwork_links": [],
    "stats": {
        "messages_scanned": 0,
        "chat_matches": 0,
        "kwork_found": 0,
        "responses_generated": 0,
        "kwork_checks": 0,
        "started_at": datetime.utcnow().isoformat()
    }
}

def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, v in DEFAULT_DATA.items():
            if k not in d:
                d[k] = v
        return d
    except Exception:
        return json.loads(json.dumps(DEFAULT_DATA))

def save_data(d):
    try:
        with data_lock:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"Save failed: {e}")

data = load_data()

def admin_only(func):
    def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if ADMIN_IDS and (not user or user.id not in ADMIN_IDS):
            update.message.reply_text("⛔ Access denied.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

def in_work_hours():
    try:
        sh, sm = map(int, WORK_START.split(":"))
        eh, em = map(int, WORK_END.split(":"))
        now = datetime.now().time()
        start, end = dtime(sh, sm), dtime(eh, em)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
    except Exception:
        return True

def cmd_start(update, context):
    update.message.reply_text(
        "👋 Bot started.\n\n"
        "/add <word> – add keyword\n"
        "/remove <word> – remove keyword\n"
        "/list – list keywords\n"
        "/clear – clear all\n"
        "/check – check Kwork now\n"
        "/pause – pause monitoring\n"
        "/resume – resume monitoring\n"
        "/set_template <text> – set response template\n"
        "/stats – statistics"
    )

@admin_only
def cmd_add(update, context):
    if not context.args:
        update.message.reply_text("Usage: /add <keyword>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw in data["keywords"]:
        update.message.reply_text("Already tracking.")
        return
    data["keywords"].append(kw)
    save_data(data)
    update.message.reply_text(f"✅ Added: {kw}")

@admin_only
def cmd_remove(update, context):
    if not context.args:
        update.message.reply_text("Usage: /remove <keyword>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw not in data["keywords"]:
        update.message.reply_text("Not found.")
        return
    data["keywords"].remove(kw)
    save_data(data)
    update.message.reply_text(f"🗑 Removed: {kw}")

@admin_only
def cmd_list(update, context):
    if not data["keywords"]:
        update.message.reply_text("No keywords.")
        return
    update.message.reply_text("📋 Keywords:\n" + "\n".join(f"• {k}" for k in data["keywords"]))

@admin_only
def cmd_clear(update, context):
    data["keywords"] = []
    save_data(data)
    update.message.reply_text("🧹 Cleared all keywords.")

@admin_only
def cmd_pause(update, context):
    data["paused"] = True
    save_data(data)
    update.message.reply_text("⏸ Monitoring paused.")

@admin_only
def cmd_resume(update, context):
    data["paused"] = False
    save_data(data)
    update.message.reply_text("▶️ Monitoring resumed.")

@admin_only
def cmd_set_template(update, context):
    if not context.args:
        update.message.reply_text(
            "Usage: /set_template <template>\n"
            "Placeholders: {title} {keyword} {price} {link}\n"
            f"Current template:\n{data['template']}"
        )
        return
    new_template = update.message.text.split(" ", 1)[1]
    data["template"] = new_template
    save_data(data)
    update.message.reply_text("✅ Template updated.")

@admin_only
def cmd_stats(update, context):
    s = data["stats"]
    update.message.reply_text(
        f"📊 Stats:\n"
        f"Scanned messages: {s['messages_scanned']}\n"
        f"Chat matches: {s['chat_matches']}\n"
        f"Kwork found: {s['kwork_found']}\n"
        f"Responses generated: {s['responses_generated']}\n"
        f"Kwork checks: {s['kwork_checks']}\n"
        f"Started: {s['started_at']}\n"
        f"Status: {'⏸ paused' if data['paused'] else '▶️ active'}\n"
        f"Keywords: {len(data['keywords'])}"
    )

@admin_only
def cmd_check(update, context):
    update.message.reply_text("🔍 Checking Kwork...")
    threading.Thread(target=kwork_check_job, args=(context,), daemon=True).start()

def handle_message(update, context):
    if data["paused"]:
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    data["stats"]["messages_scanned"] += 1
    text_lower = msg.text.lower()
    matched = [kw for kw in data["keywords"] if kw in text_lower]
    if not matched or not in_work_hours():
        if data["stats"]["messages_scanned"] % 50 == 0:
            save_data(data)
        return
    data["stats"]["chat_matches"] += 1
    save_data(data)
    kw = matched[0]
    chat_title = update.effective_chat.title or update.effective_chat.username or str(update.effective_chat.id)
    ctx_id = f"chat:{update.effective_chat.id}:{msg.message_id}"
    context.bot_data.setdefault("contexts", {})[ctx_id] = {
        "keyword": kw, "title": chat_title, "price": "", "link": ""
    }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✍️ Generate response", callback_data=f"gen:{ctx_id}")]]
    )
    target = NOTIFY_CHAT_ID or update.effective_chat.id
    try:
        context.bot.send_message(
            chat_id=target,
            text=f"🔔 Keyword «{kw}» in chat «{chat_title}»:\n\n{msg.text[:1000]}",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Notify failed: {e}")

def callback_handler(update, context):
    query = update.callback_query
    query.answer()
    if not query.data or not query.data.startswith("gen:"):
        return
    ctx_id = query.data[len("gen:"):]
    ctx = context.bot_data.get("contexts", {}).get(ctx_id)
    if not ctx:
        query.message.reply_text("⚠️ Context expired.")
        return
    try:
        response_text = data["template"].format(
            title=ctx.get("title", ""),
            keyword=ctx.get("keyword", ""),
            price=ctx.get("price", ""),
            link=ctx.get("link", "")
        )
    except Exception:
        response_text = data["template"]
    data["stats"]["responses_generated"] += 1
    save_data(data)
    query.message.reply_text(f"📝 Generated response:\n\n{response_text}")

KWORK_SEARCH_URL = "https://kwork.ru/projects"

def get_requests_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    if PROXY_URL:
        session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
    return session

def parse_price(text):
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None

def search_kwork(keyword, session):
    results = []
    try:
        resp = session.get(KWORK_SEARCH_URL, params={"a": 1, "keyword": keyword}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Kwork request error for '{keyword}': {e}")
        return results
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.want-card, div[class*='project-card'], article")
        for card in cards:
            try:
                link_tag = card.select_one("a[href*='/projects/']")
                if not link_tag:
                    continue
                href = link_tag.get("href", "")
                link = href if href.startswith("http") else f"https://kwork.ru{href}"
                title = link_tag.get_text(strip=True) or "Без названия"
                price_tag = card.select_one("[class*='price']")
                price = parse_price(price_tag.get_text(strip=True)) if price_tag else None
                results.append({"title": title, "link": link, "price": price})
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Parse error: {e}")
    return results

def kwork_check_job(context):
    if data["paused"] or not in_work_hours():
        return
    session = get_requests_session()
    data["stats"]["kwork_checks"] += 1
    for kw in list(data["keywords"]):
        try:
            orders = search_kwork(kw, session)
        except Exception as e:
            logger.error(f"Search error for '{kw}': {e}")
            continue
        for order in orders:
            link = order["link"]
            if link in data["seen_kwork_links"]:
                continue
            price = order.get("price")
            if price is not None and MIN_PRICE and price < MIN_PRICE:
                continue
            data["seen_kwork_links"].append(link)
            if len(data["seen_kwork_links"]) > 2000:
                data["seen_kwork_links"] = data["seen_kwork_links"][-1000:]
            data["stats"]["kwork_found"] += 1
            send_kwork_notification(context, kw, order)
    save_data(data)

def send_kwork_notification(context, keyword, order):
    if not NOTIFY_CHAT_ID:
        logger.warning("NOTIFY_CHAT_ID not set")
        return
    ctx_id = f"kwork:{abs(hash(order['link']))}"
    context.bot_data.setdefault("contexts", {})[ctx_id] = {
        "keyword": keyword,
        "title": order["title"],
        "price": order.get("price") or "",
        "link": order["link"]
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Generate response", callback_data=f"gen:{ctx_id}")],
        [InlineKeyboardButton("🔗 Open order", url=order["link"])]
    ])
    text = (
        f"💼 New Kwork order for «{keyword}»\n\n"
        f"{order['title']}\n"
        f"Price: {order.get('price') or 'not specified'}"
    )
    try:
        context.bot.send_message(chat_id=NOTIFY_CHAT_ID, text=text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Kwork notification failed: {e}")

def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}")

def main():
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.bot_data["contexts"] = {}
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("add", cmd_add))
    dp.add_handler(CommandHandler("remove", cmd_remove))
    dp.add_handler(CommandHandler("list", cmd_list))
    dp.add_handler(CommandHandler("clear", cmd_clear))
    dp.add_handler(CommandHandler("check", cmd_check))
    dp.add_handler(CommandHandler("pause", cmd_pause))
    dp.add_handler(CommandHandler("resume", cmd_resume))
    dp.add_handler(CommandHandler("set_template", cmd_set_template))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CallbackQueryHandler(callback_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)
    updater.job_queue.run_repeating(kwork_check_job, interval=KWORK_CHECK_INTERVAL, first=10)
    logger.info("Bot started")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
