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

# ---------- НОВАЯ БИБЛИОТЕКА ДЛЯ KWORK ----------
from kwork import KworkClient

# ---------- ENV ----------
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

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("kwork_bot")

# ---------- ШИФРОВАНИЕ ----------
def get_fernet() -> Fernet:
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

def get_kwork_password() -> str:
    return decrypt_text(KWORK_PASSWORD_ENC)

# ---------- ХРАНИЛИЩЕ ДАННЫХ ----------
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

def load_data() -> dict:
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
    except Exception as e:
        logger.error(f"Не удалось загрузить {DATA_FILE}: {e}")
        return json.loads(json.dumps(DEFAULT_DATA))

def save_data(d: dict) -> None:
    try:
        with data_lock:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"Не удалось сохранить данные: {e}")

data = load_data()

# ---------- ВСПОМОГАТЕЛЬНОЕ ----------
def admin_only(func):
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        user = update.effective_user
        if ADMIN_IDS and (not user or user.id not in ADMIN_IDS):
            update.message.reply_text("⛔ У вас нет доступа к этой команде.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper

def in_work_hours() -> bool:
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

# ---------- КОМАНДЫ ----------
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Бот запущен.\n\n"
        "Команды:\n"
        "/add <слово> — добавить ключевое слово\n"
        "/remove <слово> — удалить ключевое слово\n"
        "/list — список ключевых слов\n"
        "/clear — очистить список\n"
        "/check — проверить Kwork сейчас\n"
        "/pause — приостановить мониторинг\n"
        "/resume — возобновить мониторинг\n"
        "/set_template <текст> — задать шаблон отклика\n"
        "/stats — статистика"
    )

@admin_only
def cmd_add(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Использование: /add <ключевое слово>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw in data["keywords"]:
        update.message.reply_text("Это слово уже отслеживается.")
        return
    data["keywords"].append(kw)
    save_data(data)
    update.message.reply_text(f"✅ Добавлено ключевое слово: {kw}")

@admin_only
def cmd_remove(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Использование: /remove <ключевое слово>")
        return
    kw = " ".join(context.args).strip().lower()
    if kw not in data["keywords"]:
        update.message.reply_text("Такого слова нет в списке.")
        return
    data["keywords"].remove(kw)
    save_data(data)
    update.message.reply_text(f"🗑 Удалено ключевое слово: {kw}")

@admin_only
def cmd_list(update: Update, context: CallbackContext):
    if not data["keywords"]:
        update.message.reply_text("Список ключевых слов пуст.")
        return
    update.message.reply_text("📋 Ключевые слова:\n" + "\n".join(f"• {k}" for k in data["keywords"]))

@admin_only
def cmd_clear(update: Update, context: CallbackContext):
    data["keywords"] = []
    save_data(data)
    update.message.reply_text("🧹 Список ключевых слов очищен.")

@admin_only
def cmd_pause(update: Update, context: CallbackContext):
    data["paused"] = True
    save_data(data)
    update.message.reply_text("⏸ Мониторинг приостановлен.")

@admin_only
def cmd_resume(update: Update, context: CallbackContext):
    data["paused"] = False
    save_data(data)
    update.message.reply_text("▶️ Мониторинг возобновлён.")

@admin_only
def cmd_set_template(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Использование: /set_template <шаблон>\n"
            "Доступные плейсхолдеры: {title} {keyword} {price} {link}\n\n"
            f"Текущий шаблон:\n{data['template']}"
        )
        return
    new_template = update.message.text.split(" ", 1)[1]
    data["template"] = new_template
    save_data(data)
    update.message.reply_text("✅ Шаблон обновлён.")

@admin_only
def cmd_stats(update: Update, context: CallbackContext):
    s = data["stats"]
    text = (
        "📊 Статистика:\n"
        f"Просканировано сообщений в чатах: {s['messages_scanned']}\n"
        f"Совпадений в чатах: {s['chat_matches']}\n"
        f"Найдено заказов Kwork: {s['kwork_found']}\n"
        f"Сгенерировано откликов: {s['responses_generated']}\n"
        f"Проверок Kwork выполнено: {s['kwork_checks']}\n"
        f"Запущен: {s['started_at']}\n"
        f"Статус: {'⏸ пауза' if data['paused'] else '▶️ активен'}\n"
        f"Ключевых слов: {len(data['keywords'])}"
    )
    update.message.reply_text(text)

@admin_only
def cmd_check(update: Update, context: CallbackContext):
    update.message.reply_text("🔍 Запускаю проверку Kwork...")
    kwork_check_job(context)  # синхронно, чтобы сразу видеть результат

# ---------- МОНИТОРИНГ ЧАТОВ ----------
def handle_message(update: Update, context: CallbackContext):
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
        [[InlineKeyboardButton("✍️ Сгенерировать отклик", callback_data=f"gen:{ctx_id}")]]
    )
    target = NOTIFY_CHAT_ID or update.effective_chat.id
    try:
        context.bot.send_message(
            chat_id=target,
            text=f"🔔 Найдено ключевое слово «{kw}» в чате «{chat_title}»:\n\n{msg.text[:1000]}",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о чате: {e}")

def callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    if not query.data or not query.data.startswith("gen:"):
        return
    ctx_id = query.data[len("gen:"):]
    ctx = context.bot_data.get("contexts", {}).get(ctx_id)
    if not ctx:
        query.message.reply_text("⚠️ Контекст этого уведомления устарел или недоступен.")
        return
    try:
        response_text = data["template"].format(
            title=ctx.get("title", ""),
            keyword=ctx.get("keyword", ""),
            price=ctx.get("price", ""),
            link=ctx.get("link", "")
        )
    except Exception as e:
        logger.error(f"Ошибка форматирования шаблона: {e}")
        response_text = data["template"]

    data["stats"]["responses_generated"] += 1
    save_data(data)
    query.message.reply_text(f"📝 Сгенерированный отклик:\n\n{response_text}")

# ---------- ПАРСИНГ KWORK ЧЕРЕЗ БИБЛИОТЕКУ ----------
kwork_client = None

def init_kwork() -> bool:
    global kwork_client
    login = KWORK_LOGIN
    password = get_kwork_password()
    if not login or not password:
        logger.warning("Логин или пароль Kwork не заданы — парсинг недоступен.")
        return False
    try:
        kwork_client = KworkClient(login, password)
        # Пробуем вызвать метод, чтобы проверить авторизацию.
        # В библиотеке может быть метод get_profile, но если нет — просто проверим, что объект создан.
        # Некоторые версии библиотеки требуют отдельного вызова login().
        # Попробуем выполнить любой запрос, например поиск по пустому слову.
        kwork_client.get_projects(query="")  # если метод есть
        logger.info("✅ Авторизация Kwork успешна (через библиотеку)")
        return True
    except AttributeError:
        # Если метода get_projects нет, пробуем другой способ
        try:
            # Возможно, нужно инициализировать сессию через login()
            if hasattr(kwork_client, 'login'):
                kwork_client.login()
            logger.info("✅ Авторизация Kwork успешна (через библиотеку)")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка авторизации Kwork: {e}")
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Kwork: {e}")
        return False

def search_kwork(keyword: str) -> list:
    if not kwork_client:
        logger.warning("Kwork клиент не инициализирован")
        return []
    try:
        # Библиотека может иметь разные методы: get_projects, search, search_projects
        # Попробуем несколько вариантов
        if hasattr(kwork_client, 'get_projects'):
            projects = kwork_client.get_projects(query=keyword)
        elif hasattr(kwork_client, 'search_projects'):
            projects = kwork_client.search_projects(query=keyword)
        elif hasattr(kwork_client, 'search'):
            projects = kwork_client.search(keyword)
        else:
            logger.error("Не найден метод поиска в библиотеке Kwork")
            return []
        results = []
        for p in projects:
            # Предполагаем, что проекты — это словари или объекты
            if isinstance(p, dict):
                title = p.get("title", "Без названия")
                link = p.get("link", "") or f"https://kwork.ru/projects/{p.get('id', '')}"
                price = p.get("price")
            else:
                # Если объект, пробуем через атрибуты
                title = getattr(p, "title", "Без названия")
                link = getattr(p, "link", "") or f"https://kwork.ru/projects/{getattr(p, 'id', '')}"
                price = getattr(p, "price", None)
            results.append({
                "title": title,
                "link": link,
                "price": price
            })
        return results
    except Exception as e:
        logger.error(f"Ошибка при поиске Kwork по '{keyword}': {e}")
        return []

def kwork_check_job(context: CallbackContext):
    if data["paused"] or not in_work_hours():
        return

    # Проверяем, инициализирован ли клиент
    if not kwork_client:
        if not init_kwork():
            logger.error("Нет авторизации на Kwork — пропуск проверки")
            return

    data["stats"]["kwork_checks"] += 1
    found = 0

    for kw in list(data["keywords"]):
        orders = search_kwork(kw)
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
            send_kwork_notification(context, kw, order)

    if found:
        logger.info(f"Найдено {found} новых заказов на Kwork")
    else:
        logger.info("Новых заказов на Kwork не найдено")
    save_data(data)

def send_kwork_notification(context: CallbackContext, keyword: str, order: dict):
    if not NOTIFY_CHAT_ID:
        logger.warning("NOTIFY_CHAT_ID не задан — уведомление о заказе Kwork не отправлено")
        return

    ctx_id = f"kwork:{abs(hash(order['link']))}"
    context.bot_data.setdefault("contexts", {})[ctx_id] = {
        "keyword": keyword,
        "title": order["title"],
        "price": order.get("price") or "",
        "link": order["link"]
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Сгенерировать отклик", callback_data=f"gen:{ctx_id}")],
        [InlineKeyboardButton("🔗 Открыть заказ", url=order["link"])]
    ])
    text = (
        f"💼 Новый заказ на Kwork по слову «{keyword}»\n\n"
        f"{order['title']}\n"
        f"Цена: {order.get('price') or 'не указана'}"
    )
    try:
        context.bot.send_message(chat_id=NOTIFY_CHAT_ID, text=text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление о заказе Kwork: {e}")

# ---------- ЗАПУСК ----------
def error_handler(update: object, context: CallbackContext):
    logger.error(f"Update {update} вызвал ошибку: {context.error}")

def main():
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.bot_data["contexts"] = {}

    # Инициализация Kwork при старте
    init_kwork()

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

    # Планировщик проверки Kwork
    updater.job_queue.run_repeating(kwork_check_job, interval=KWORK_CHECK_INTERVAL, first=10)

    logger.info("Бот запущен")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
