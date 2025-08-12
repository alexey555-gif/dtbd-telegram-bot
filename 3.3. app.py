# app.py
# Бот: спрашивает город, ЖК, описание, год сдачи → шлёт в GPT-5 →
# возвращает ровно 5 микро-ЦА (JTBD/DTBD) с названиями, триггерами и т.д.
import os, json, asyncio, logging
from contextlib import asynccontextmanager
from html import escape
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, Response
from http import HTTPStatus

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

from openai import OpenAI

# -------- ЛОГИ --------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jtbd-bot")

# -------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ --------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN")  # любой длинный пароль

if not BOT_TOKEN:
    raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана")
if not OPENAI_API_KEY:
    raise RuntimeError("Переменная OPENAI_API_KEY не задана")

# -------- OpenAI клиент --------
oai = OpenAI(api_key=OPENAI_API_KEY)

# -------- Telegram Application --------
application = Application.builder().token(BOT_TOKEN).build()

# Состояния диалога
CITY, COMPLEX, DESCRIPTION, YEAR = range(4)

def build_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "audiences": {
                "type": "array",
                "minItems": 5, "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "main_job": {"type": "string"},
                        "trigger": {"type": "string"},
                        "critical_subtasks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 3, "maxItems": 6
                        },
                        "digital_marketing_recos": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 4, "maxItems": 8
                        }
                    },
                    "required": [
                        "name","description","main_job","trigger",
                        "critical_subtasks","digital_marketing_recos"
                    ],
                    "additionalProperties": False
                }
            }
        },
        "required": ["audiences"],
        "additionalProperties": False
    }

def system_prompt() -> str:
    return (
        "Ты эксперт по сегментации жилой недвижимости. Используй Jobs To Be Done "
        "(какой прогресс/результат хочет клиент) и Dreams To Be Done (какую мечту "
        "и стиль жизни он реализует). Сгенерируй РОВНО 5 микро-ЦА для конкретного ЖК. "
        "Имена — 2–3 слова, по-русски, запоминающиеся. Для каждой ЦА верни: "
        "name, description, main_job, trigger, critical_subtasks[], digital_marketing_recos[]. "
        "Учитывай город, описание проекта и год сдачи (сроки, риски/выгоды)."
    )

async def call_gpt5(city: str, complex_name: str, desc: str, year: str) -> Optional[Dict[str, Any]]:
    user_prompt = (
        f"Город: {city}\nЖилой комплекс: {complex_name}\n"
        f"Описание проекта: {desc}\nГод сдачи: {year}\n\n"
        "Верни строго JSON по схеме audience_pack."
    )
    try:
        resp = oai.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "audience_pack",
                    "schema": build_schema(),
                    "strict": True
                }
            },
            temperature=0.6,
            max_tokens=1800
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        return None

def format_audience(i: int, a: Dict[str, Any]) -> str:
    bullets_sub = "".join([f"• {escape(x)}\n" for x in a["critical_subtasks"]])
    bullets_dm  = "".join([f"• {escape(x)}\n" for x in a["digital_marketing_recos"]])
    return (
        f"<b>{i}. {escape(a['name'])}</b>\n"
        f"<b>Описание:</b> {escape(a['description'])}\n"
        f"<b>Главная задача (JTBD):</b> {escape(a['main_job'])}\n"
        f"<b>Триггер:</b> {escape(a['trigger'])}\n"
        f"<b>Критические подзадачи:</b>\n{bullets_sub}"
        f"<b>Рекомендации для digital:</b>\n{bullets_dm}"
    )

async def send_long(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE):
    # Телеграм ограничивает ~4096 символов — режем по абзацам
    LIMIT = 3500
    parts = []
    while len(text) > LIMIT:
        cut = text.rfind("\n", 0, LIMIT)
        cut = cut if cut != -1 else LIMIT
        parts.append(text[:cut])
        text = text[cut:]
    parts.append(text)
    for p in parts:
        await context.bot.send_message(chat_id=chat_id, text=p, parse_mode="HTML")

# --------- HANDLERS ---------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("1/4 Введите <b>город</b>:", parse_mode="HTML")
    return CITY

async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["city"] = update.message.text.strip()
    await update.message.reply_text("2/4 Введите <b>название ЖК</b>:", parse_mode="HTML")
    return COMPLEX

async def on_complex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["complex"] = update.message.text.strip()
    await update.message.reply_text("3/4 Кратко опишите проект (класс, локация, фишки):", parse_mode="HTML")
    return DESCRIPTION

async def on_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["desc"] = update.message.text.strip()
    await update.message.reply_text("4/4 Укажите <b>год сдачи</b> (например, 2027):", parse_mode="HTML")
    return YEAR

async def on_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["year"] = update.message.text.strip()
    await update.message.reply_text("Думаю над сегментами… ⏳")

    data = await call_gpt5(
        context.user_data["city"],
        context.user_data["complex"],
        context.user_data["desc"],
        context.user_data["year"]
    )

    if not data:
        await update.message.reply_text("Не удалось получить ответ от модели. Попробуйте /start ещё раз.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    for i, a in enumerate(data["audiences"], start=1):
        await send_long(chat_id, format_audience(i, a), context)

    await update.message.reply_text("Готово! Чтобы начать заново — /start")
    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, отменил. Начать заново — /start")
    return ConversationHandler.END

def setup_handlers(app: Application):
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_city)],
            COMPLEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_complex)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_desc)],
            YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_year)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cmd_cancel))

setup_handlers(application)

# --------- FastAPI + Webhook ---------
WEBHOOK_PATH = "/webhook"  # не меняй, иначе надо менять и set_webhook

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Явно инициализируем/стартуем PTB-приложение
    await application.initialize()
    # Настроим вебхук только если PUBLIC_URL задан
    if PUBLIC_URL:
        url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=url, secret_token=SECRET_TOKEN)
        log.info("Webhook установлен: %s", url)
    else:
        log.warning("PUBLIC_URL не задан. Установи домен Railway и переменную PUBLIC_URL, затем перезапусти деплой.")
    await application.start()
    try:
        yield
    finally:
        await application.stop()
        await application.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Проверяем секрет (если задан)
    if SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != SECRET_TOKEN:
            return Response(status_code=HTTPStatus.FORBIDDEN)
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return Response(status_code=HTTPStatus.OK)
