import logging
import csv
import gspread
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, ContextTypes
)
from oauth2client.service_account import ServiceAccountCredentials

# === НАСТРОЙКИ ===
TOKEN = "8109187093:AAE3YTLbFlz3x-nq-J5kM_M-iMXmmwPNfF8"
ADMIN_IDS = {1333437457}  # Ваш Telegram ID
bot_username = "EnergoServiceBot"   # Имя вашего бота
CSV_FILE = "applications.csv"

GOOGLE_CREDS_FILE = "cultivated-age-438106-i2-39cf553124d7.json"
GOOGLE_SHEET_ID = "1ZJkQJjlPZELzTnjCqQhF5IDMmUWF-nG-yO2kzbK0G70"  # Только ID!

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("workbot")

ASK_NAME, ASK_CHOICE, ASK_REASON = range(3)

# === Информация о каждой вакансии: { job_key: {...} }
jobs_context = {}  # основная информация
jobs_applications = {}  # заявки по каждой вакансии {job_key: [user_dict, ...]}

def get_worksheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, SCOPE)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID).sheet1

async def save_application(job_key, data, choice, reason):
    row = [
        data.get("fio", ""),
        choice,
        reason,
        data.get("work_title", ""),
        data.get("city", ""),
        data.get("description", "")
    ]
    # В CSV
    with open(CSV_FILE, "a", newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(row)
    # В Google Sheets
    try:
        ws = get_worksheet()
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
    # В списке участников
    jobs_applications.setdefault(job_key, []).append({
        "fio": data.get("fio", ""),
        "choice": choice,
        "reason": reason
    })


async def notify_admins_about_job(context, job_key):
    job = jobs_context.get(job_key)
    if not job:
        return
    applications = jobs_applications.get(job_key, [])
    go_users = [a["fio"] for a in applications if a["choice"] == "Еду"]
    nogo_users = [f'{a["fio"]} – {a["reason"]}' for a in applications if a["choice"] == "Не еду"]
    text = f"Завершена запись на:\n\n" \
           f"Работы: {job['work_title']}\nГород: {job['city']}\n\n"
    text += "💪 Записались:\n" + ("\n".join(go_users) if go_users else "Никто") + "\n\n"
    if nogo_users:
        text += "❌ Отказались:\n" + "\n".join(nogo_users)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logger.error(f"Cannot notify admin {admin_id}: {e}")

# ====== /post ======
async def post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("У вас нет прав публикации.")
        return
    if not context.args:
        await update.message.reply_text("Формат: /post Название_работ;Город;Описание;дд.мм.гггг чч:мм")
        return
    try:
        text = " ".join(context.args)
        parts = [x.strip() for x in text.split(";")]
        if len(parts) < 4:
            raise Exception("Проверьте формат!")
        work_title, city, description, deadline_raw = parts
        deadline = datetime.strptime(deadline_raw, "%d.%m.%Y %H:%M")
        if deadline < datetime.now():
            await update.message.reply_text("Ошибка: время завершения не может быть в прошлом!")
            return
    except Exception:
        await update.message.reply_text("Формат: /post Название_работ;Город;Описание;дд.мм.гггг чч:ммn"
                                       "Пример: /post Монтаж ПС;Москва;Установка оборудования;20.06.2024 19:00")
        return

    chat_id = update.message.chat_id

    job_key = str(uuid4())[:8]
    jobs_context[job_key] = {
        "work_title": work_title,
        "city": city,
        "description": description,
        "deadline": deadline.strftime("%d.%m.%Y %H:%M")
    }
    link = f"https://t.me/{bot_username}?start=apply_{job_key}"
    msg = (
        f"Работы: {work_title}\n"
        f"Город: {city}\n"
        f"Описание: {description}\n"
        f"Запись до: {deadline.strftime('%d.%m.%Y %H:%M')}\n"
        f"Кто готов выехать?"
    )
    keyboard = [[InlineKeyboardButton("Записаться на работы", url=link)]]

    # Отправляем пост НЕ в режиме ответа:
    sent_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'  # если нужно форматирование
    )
    post_message_id = sent_msg.message_id

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete command message: {e}")

    delay = (deadline - datetime.now()).total_seconds()
    context.application.create_task(
        delayed_notification(context, job_key, delay, chat_id, post_message_id)
    )

async def delayed_notification(context, job_key, delay, chat_id, message_id):
    await asyncio.sleep(delay)
    try:
        # Убираем кнопку/markup по истечении времени
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        # НЕ отправляем никакое новое сообщение в чат!
    except Exception as e:
        logger.warning(f"Edit post error: {e}")
    await notify_admins_about_job(context, job_key)

# ====== /start ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("apply_"):
        job_key = args[0].split("_", 1)[1]
        job = jobs_context.get(job_key)
        if job:
            context.user_data["job_key"] = job_key
            context.user_data.update(job)
        else:
            await update.message.reply_text("Ошибка: работа не найдена. Повторите переход по свежей ссылке.")
            return ConversationHandler.END
        await update.message.reply_text(
            "Пожалуйста, введите ваше ФИО:",
            reply_markup=ReplyKeyboardRemove()
        )
        return ASK_NAME
    await update.message.reply_text(
        "Здравствуйте! Этот бот поможет вам записаться на работы."
    )
    return ConversationHandler.END

# ====== Получаем ФИО ======
async def ask_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fio = update.message.text.strip()
    context.user_data["fio"] = fio
    keyboard = [
        [InlineKeyboardButton("Я поеду", callback_data="go")],
        [InlineKeyboardButton("Не поеду", callback_data="nogo")]
    ]
    await update.message.reply_text(
        "Вы собираетесь на работы?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASK_CHOICE

# ====== Обрабатываем выбор ======
async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    choice = query.data
    await query.answer()
    job_key = context.user_data.get("job_key")
    if choice == "go":
        await save_application(job_key, context.user_data, "Еду", "")
        await query.message.reply_text("Спасибо, вы записаны на работы!")
        return ConversationHandler.END
    elif choice == "nogo":
        await query.message.reply_text("Пожалуйста, укажите причину отказа:")
        return ASK_REASON

# ====== Получаем причину отказа ======
async def on_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    job_key = context.user_data.get("job_key")
    await save_application(job_key, context.user_data, "Не еду", reason)
    await update.message.reply_text("Спасибо, отказ зафиксирован!")
    return ConversationHandler.END

# ====== Отмена ======
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Операция отменена.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_choice)],
            ASK_CHOICE: [CallbackQueryHandler(on_choice, pattern="^(go|nogo)$")],
            ASK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_reason)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    app.add_handler(CommandHandler("post", post))
    app.add_handler(conv)
    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()