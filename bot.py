import os
import re
import json
import logging
import threading
from datetime import datetime, time as dtime

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, CallbackQueryHandler,
    CallbackContext, Filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID")
MIN_PRICE = float(os.getenv("MIN_PRICE", "0") or 0)
WORK_START = os.getenv("WORK_START", "00:00")
WORK_END = os.getenv("WORK_END", "23:59")
KWORK_CHECK_INTERVAL = int(os.getenv("KWORK_CHECK_INTERVAL", "300"))
DATA_FILE = os.getenv("DATA_FILE", "data.json")
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger("kwork_bot")

DEFAULT_DATA = {
    "keywords": [],
    "template": "Здравствуйте! Заинтересовал заказ «{title}». Готов приступить.",
    "paused": False,
    "seen_kwork_links": [],
    "stats": {"messages_scanned": 0, "chat_matches": 0, "kwork_found": 0, "kwork_checks": 0}
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, ensure_ascii=False, indent=2)
        return DEFAULT_DATA
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

data = load_data()

def admin_only(func):
    def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if ADMIN_IDS and (not user or user.id not in ADMIN_IDS):
            update.message.reply_text("⛔ Доступ запрещён.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

def in_work_hours():
    try:
        sh, sm = map(int, WORK_START.split(":"))
        eh, em = map(int, WORK_END.split(":"))
        now = datetime.now().time()
        start, end = dtime(sh, sm), dtime(eh, em)
        return start <= now <= end if start <= end else now >= start or now <= end
    except:
        return True

def cmd_start(update, context):
    update.message.reply_text(
        "👋 Бот запущен.\n"
        "/add слово — добавить\n"
        "/remove слово — удалить\n"
        "/list — список\n"
        "/clear — очистить\n"
        "/check — проверить Kwork\n"
        "/pause — пауза\n"
        "/resume — продолжить\n"
        "/stats — статистика"
    )

@admin_only
def cmd_add(update, context):
    if not context.args:
        update.message.reply_text("Использование: /add <слово>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw in data["keywords"]:
        update.message.reply_text("Уже есть.")
        return
    data["keywords"].append(kw)
    save_data(data)
    update.message.reply_text(f"✅ Добавлено: {kw}")

@admin_only
def cmd_remove(update, context):
    if not context.args:
        update.message.reply_text("Использование: /remove <слово>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw not in data["keywords"]:
        update.message.reply_text("Нет такого.")
        return
    data["keywords"].remove(kw)
    save_data(data)
    update.message.reply_text(f"🗑 Удалено: {kw}")

@admin_only
def cmd_list(update, context):
    if not data["keywords"]:
        update.message.reply_text("Список пуст.")
        return
    update.message.reply_text("📋 Ключевые слова:\n" + "\n".join(f"• {k}" for k in data["keywords"]))

@admin_only
def cmd_clear(update, context):
    data["keywords"] = []
    save_data(data)
    update.message.reply_text("🧹 Очищено.")

@admin_only
def cmd_pause(update, context):
    data["paused"] = True
    save_data(data)
    update.message.reply_text("⏸ Пауза.")

@admin_only
def cmd_resume(update, context):
    data["paused"] = False
    save_data(data)
    update.message.reply_text("▶️ Продолжаем.")

@admin_only
def cmd_stats(update, context):
    s = data["stats"]
    update.message.reply_text(
        f"📊 Статистика:\n"
        f"Проверок Kwork: {s['kwork_checks']}\n"
        f"Найдено заказов: {s['kwork_found']}\n"
        f"Ключевых слов: {len(data['keywords'])}\n"
        f"Статус: {'⏸ пауза' if data['paused'] else '▶️ активен'}"
    )

@admin_only
def cmd_check(update, context):
    update.message.reply_text("🔍 Проверяю Kwork...")
    kwork_check_job(context)

def search_kwork_public(keyword):
    """Парсит публичные проекты с Kwork (без авторизации)"""
    try:
        resp = requests.get(
            "https://kwork.ru/projects",
            params={"a": 1, "keyword": keyword},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for card in soup.select("div.want-card, div[class*='project-card'], article, div.project-item"):
            link_tag = card.select_one("a[href*='/projects/']")
            if not link_tag:
                continue
            href = link_tag.get("href", "")
            link = href if href.startswith("http") else f"https://kwork.ru{href}"
            title = link_tag.get_text(strip=True) or "Без названия"
            price_tag = card.select_one("[class*='price']")
            price = None
            if price_tag:
                price_text = price_tag.get_text(strip=True)
                digits = re.sub(r"[^\d]", "", price_text)
                if digits:
                    try:
                        price = float(digits)
                    except:
                        price = None
            results.append({"title": title, "link": link, "price": price})
        return results
    except Exception as e:
        logger.error(f"Ошибка поиска Kwork: {e}")
        return []

def kwork_check_job(context):
    if data["paused"] or not in_work_hours():
        return

    data["stats"]["kwork_checks"] += 1
    found = 0

    for kw in list(data["keywords"]):
        orders = search_kwork_public(kw)
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
            found += 1

            # Отправка уведомления
            if NOTIFY_CHAT_ID:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Открыть заказ", url=order["link"])]
                ])
                try:
                    context.bot.send_message(
                        chat_id=NOTIFY_CHAT_ID,
                        text=f"💼 Новый заказ на Kwork по слову «{kw}»\n\n{order['title']}\nЦена: {order.get('price') or 'не указана'}",
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки: {e}")

    if found:
        logger.info(f"Найдено {found} новых заказов")
    else:
        logger.info("Новых заказов нет")
    save_data(data)

def error_handler(update, context):
    logger.error(f"Ошибка: {context.error}")

def main():
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("add", cmd_add))
    dp.add_handler(CommandHandler("remove", cmd_remove))
    dp.add_handler(CommandHandler("list", cmd_list))
    dp.add_handler(CommandHandler("clear", cmd_clear))
    dp.add_handler(CommandHandler("check", cmd_check))
    dp.add_handler(CommandHandler("pause", cmd_pause))
    dp.add_handler(CommandHandler("resume", cmd_resume))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_error_handler(error_handler)

    updater.job_queue.run_repeating(kwork_check_job, interval=KWORK_CHECK_INTERVAL, first=10)
    logger.info("Бот запущен (публичный парсинг Kwork)")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
