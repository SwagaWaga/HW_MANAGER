import os
import asyncio
import logging
import html
from datetime import date, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from db import (
    init_db,
    upsert_user,
    get_pending_tasks,
    add_task,
    mark_task_done,
    delete_task,
    get_today_tasks,
    get_overdue_tasks,
    delete_all_tasks,
    get_materials_for_task,
    get_all_users
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Loads secret environment variables
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEB_APP_URL = os.getenv('WEB_APP_URL', 'https://asbestos-grocery-restore-minute.trycloudflare.com')

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# Russian localization helpers
RU_WEEKDAYS = {
    0: "в понедельник",
    1: "во вторник",
    2: "в среду",
    3: "в четверг",
    4: "в пятницу",
    5: "в субботу",
    6: "в воскресенье"
}

RU_WEEKDAYS_NOMINATIVE = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье"
}

RU_MONTHS = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

SUBJECT_ICONS = {
    "алгебр": "📐",
    "геометр": "📐",
    "матем": "🔢",
    "math": "📐",
    "русск": "📖",
    "литератур": "📚",
    "физик": "⚡",
    "physics": "⚡",
    "хими": "🧪",
    "chemistry": "🧪",
    "биолог": "🧬",
    "biology": "🧬",
    "истори": "🏛️",
    "history": "🏛️",
    "обществ": "⚖️",
    "англ": "🇬🇧",
    "english": "🇬🇧",
    "информ": "💻",
    "географ": "🌍",
    "физ-р": "⚽",
    "физкульт": "⚽",
    "обж": "🦺"
}

def get_subject_icon(subject: str) -> str:
    s = (subject or "").lower()
    for key, icon in SUBJECT_ICONS.items():
        if key in s:
            return icon
    return "📝"

def get_portal_keyboard(button_text: str = "🚀 Открыть ДЗ Портал") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, web_app=WebAppInfo(url=WEB_APP_URL))
    ]])

def record_user_activity(from_user):
    if not from_user:
        return
    nick = f"@{from_user.username}" if from_user.username else (from_user.first_name or "Ученик")
    full_name = f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()
    upsert_user(from_user.id, telegram_nick=nick, full_name=full_name)

def build_tomorrow_notification(user_id: int) -> tuple[str, bool]:
    """
    Builds tomorrow's homework notification text and returns (formatted_text, has_tasks).
    """
    tomorrow = date.today() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    pending = get_pending_tasks(user_id)
    tomorrow_tasks = [t for t in pending if str(t.get('due_date')) == tomorrow_str]

    weekday_prep = RU_WEEKDAYS.get(tomorrow.weekday(), "завтра")
    weekday_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
    date_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"

    if tomorrow_tasks:
        lines = [
            f"🔔 <b>Напоминание о домашнем задании {weekday_prep}!</b>",
            f"📅 <b>{weekday_nom}, {date_label}</b>",
            "",
            f"📚 <b>Задания на завтра ({len(tomorrow_tasks)}):</b>"
        ]
        for idx, t in enumerate(tomorrow_tasks, 1):
            icon = get_subject_icon(t.get('subject', ''))
            subj = html.escape(str(t.get('subject', 'Предмет')).strip())
            desc = html.escape(str(t.get('description', '')).strip())
            lines.append(f"  {idx}. {icon} <b>{subj}</b>: {desc}")

        lines.append("")
        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, просмотреть прикреплённые фото или отметить сделанным 👇")
        return "\n".join(lines), True
    else:
        text = (
            f"🎉 <b>Отличные новости!</b>\n\n"
            f"На завтра (<i>{weekday_nom}, {date_label}</i>) нет невыполненных заданий.\n\n"
            f"Отдыхайте или загляните в ДЗ Портал, чтобы подготовиться к другим дням 👇"
        )
        return text, False

async def send_evening_reminder():
    """
    Scheduled job sending tomorrow's pending homework notifications
    to all registered students.
    """
    if not bot:
        logger.warning("⏰ send_evening_reminder skipped: Telegram Bot instance is not initialized")
        return

    logger.info("⏰ Running scheduled evening reminder for tomorrow's homework...")
    users = get_all_users()
    if not users:
        # Fallback to configured user ID
        users = [{"id": 5783100321}]

    sent_count = 0
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        try:
            text, has_tasks = build_tomorrow_notification(uid)
            if has_tasks:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=get_portal_keyboard("🚀 Открыть ДЗ Портал")
                )
                sent_count += 1
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Could not send evening reminder to user {uid}: {e}")

    logger.info(f"⏰ Evening reminder finished: sent {sent_count} notifications.")

@dp.callback_query()
async def handle_done_callback(callback: CallbackQuery):
    if callback.data.startswith("done:"):
        task_id = int(callback.data.split(":")[1])
        mark_task_done(task_id, callback.from_user.id)
        await callback.answer("Задание выполнено! 🎉")
        await callback.message.edit_text(f"Задание #{task_id} выполнено! ✅")

@dp.message(CommandStart())
async def handle_start(message: Message):
    record_user_activity(message.from_user)
    user_name = message.from_user.first_name or "ученик"
    text = (
        f"👋 Привет, <b>{html.escape(user_name)}</b>!\n\n"
        f"🎒 Добро пожаловать в <b>ДЗ Портал 10-го класса</b>.\n\n"
        f"Здесь собраны все актуальные домашние задания, дедлайны, фото учебников и конспектов.\n\n"
        f"Нажмите кнопку ниже, чтобы открыть приложение 👇"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_portal_keyboard("🚀 Открыть ДЗ Портал"))

@dp.message(Command("remind", "notify", "tomorrow", "zavtra"))
async def handle_remind(message: Message):
    """
    On-demand command for testing and checking tomorrow's homework notification.
    """
    record_user_activity(message.from_user)
    text, has_tasks = build_tomorrow_notification(message.from_user.id)
    btn_text = "🚀 Открыть ДЗ Портал" if has_tasks else "🎒 Открыть ДЗ Портал"
    await message.answer(text, parse_mode="HTML", reply_markup=get_portal_keyboard(btn_text))

@dp.message(Command("notify_all", "broadcast_reminders"))
async def handle_notify_all(message: Message):
    """
    Admin command to immediately broadcast tomorrow's notifications to all students.
    """
    record_user_activity(message.from_user)
    users = get_all_users()
    if not users:
        users = [{"id": message.from_user.id}]

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    await message.answer("📢 Запускаю рассылку напоминаний на завтра всем ученикам...")
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        try:
            text, has_tasks = build_tomorrow_notification(uid)
            if has_tasks:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=get_portal_keyboard("🚀 Открыть ДЗ Портал")
                )
                sent_count += 1
                await asyncio.sleep(0.05)
            else:
                skipped_count += 1
        except Exception as e:
            failed_count += 1
            logger.warning(f"Broadcast failed for user {uid}: {e}")

    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Отправлено: <b>{sent_count}</b>\n"
        f"• Без заданий на завтра (пропущено): <b>{skipped_count}</b>\n"
        f"• Ошибок отправки: <b>{failed_count}</b>",
        parse_mode="HTML",
        reply_markup=get_portal_keyboard()
    )

@dp.message(Command("today"))
async def handle_today_tasks(message: Message):
    record_user_activity(message.from_user)
    today = date.today()
    today_str = today.isoformat()
    tasks = get_today_tasks(message.from_user.id, today_str)

    weekday_nom = RU_WEEKDAYS_NOMINATIVE.get(today.weekday(), "Сегодня")
    date_label = f"{today.day} {RU_MONTHS[today.month]}"

    if not tasks:
        await message.answer(
            f"🎉 <b>На сегодня ({weekday_nom}, {date_label}) нет невыполненных заданий!</b>\n\n"
            f"Отдыхайте или загляните в ДЗ Портал 👇",
            parse_mode="HTML",
            reply_markup=get_portal_keyboard()
        )
    else:
        lines = [
            f"📋 <b>Задания на сегодня ({weekday_nom}, {date_label}) [{len(tasks)}]:</b>",
            ""
        ]
        for idx, t in enumerate(tasks, 1):
            icon = get_subject_icon(t.get('subject', ''))
            subj = html.escape(str(t.get('subject', 'Предмет')).strip())
            desc = html.escape(str(t.get('description', '')).strip())
            lines.append(f"{idx}. {icon} <b>{subj}</b>: {desc}")

        lines.append("")
        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал 👇")
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_portal_keyboard())

@dp.message(Command("overdue"))
async def handle_overdue(message: Message):
    record_user_activity(message.from_user)
    today_str = date.today().isoformat()
    overdue = get_overdue_tasks(message.from_user.id, today_str)

    if not overdue:
        await message.answer("✨ <b>Просроченных заданий нет! Вы молодец!</b>", parse_mode="HTML", reply_markup=get_portal_keyboard())
    else:
        lines = [f"⚠️ <b>Просроченные задания ({len(overdue)}):</b>", ""]
        for idx, t in enumerate(overdue, 1):
            icon = get_subject_icon(t.get('subject', ''))
            subj = html.escape(str(t.get('subject', 'Предмет')).strip())
            desc = html.escape(str(t.get('description', '')).strip())
            due = t.get('due_date', '')
            lines.append(f"{idx}. {icon} <b>{subj}</b>: {desc} <i>(дедлайн: {due})</i>")

        lines.append("")
        lines.append("🚀 Откройте ДЗ Портал, чтобы закрыть задания 👇")
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_portal_keyboard())

@dp.message(Command("priority", "pending"))
async def handle_priority(message: Message):
    record_user_activity(message.from_user)
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    tasks = get_pending_tasks(message.from_user.id)

    if not tasks:
        await message.answer("🎉 <b>У вас нет невыполненных заданий! Всё чисто.</b>", parse_mode="HTML", reply_markup=get_portal_keyboard())
        return

    p_today, p_tomorrow, p_other = [], [], []
    for t in tasks:
        d = str(t.get('due_date', ''))
        if d <= today_str:
            p_today.append(t)
        elif d == tomorrow_str:
            p_tomorrow.append(t)
        else:
            p_other.append(t)

    lines = ["📋 <b>Список невыполненных заданий:</b>\n"]
    if p_today:
        lines.append(f"⚠️ <b>Сегодня / Просрочено ({len(p_today)}):</b>")
        for t in p_today:
            lines.append(f"• {get_subject_icon(t.get('subject', ''))} <b>{html.escape(str(t.get('subject', '')))}</b>: {html.escape(str(t.get('description', '')))}")
        lines.append("")

    if p_tomorrow:
        lines.append(f"🔔 <b>На завтра ({len(p_tomorrow)}):</b>")
        for t in p_tomorrow:
            lines.append(f"• {get_subject_icon(t.get('subject', ''))} <b>{html.escape(str(t.get('subject', '')))}</b>: {html.escape(str(t.get('description', '')))}")
        lines.append("")

    if p_other:
        lines.append(f"📅 <b>На другие дни ({len(p_other)}):</b>")
        for t in p_other:
            lines.append(f"• {get_subject_icon(t.get('subject', ''))} <b>{html.escape(str(t.get('subject', '')))}</b> ({t.get('due_date')}): {html.escape(str(t.get('description', '')))}")
        lines.append("")

    lines.append("🚀 Открыть приложение:")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_portal_keyboard())

@dp.message(Command("add"))
async def handle_add(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args:
        await message.answer("Использование: /add <предмет> <ГГГГ-ММ-ДД> <описание>\nПример: /add Алгебра 2026-11-16 Стр. 45 № 1-5")
        return

    args = command.args.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("Использование: /add <предмет> <ГГГГ-ММ-ДД> <описание>")
        return

    subject, due_date, description = args[0], args[1], args[2]
    user = message.from_user
    nick = f"@{user.username}" if user.username else (user.first_name or "Ученик")
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    task_id = add_task(user.id, subject, description, due_date, telegram_nick=nick, full_name=full_name)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отметить сделанным", callback_data=f"done:{task_id}")],
        [InlineKeyboardButton(text="🚀 Открыть ДЗ Портал", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])

    await message.answer(
        f"✅ Добавлено #{task_id}: [{subject}] {description} (Дедлайн: {due_date})",
        reply_markup=keyboard
    )

@dp.message(Command("done"))
async def handle_done(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /done <номер_задания>\nПример: /done 2")
        return

    task_id = int(command.args)
    mark_task_done(task_id, message.from_user.id)
    await message.answer(f"🎉 Отлично! Задание #{task_id} отмечено выполненным.", reply_markup=get_portal_keyboard())

@dp.message(Command("delete"))
async def handle_delete(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /delete <номер_задания>\nПример: /delete 2")
        return

    task_id = int(command.args)
    delete_task(task_id, message.from_user.id)
    await message.answer(f"🗑️ Задание #{task_id} удалено.")

@dp.message(Command("delete_all"))
async def handle_delete_all(message: Message):
    record_user_activity(message.from_user)
    delete_all_tasks(message.from_user.id)
    await message.answer("🗑️ Все ваши задания успешно удалены.")

@dp.message(Command("materials"))
async def handle_materials(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await message.answer("Использование: /materials <номер_задания>")
        return

    task_id = int(command.args)
    materials = get_materials_for_task(task_id)

    if not materials:
        await message.answer(f"К заданию #{task_id} пока не прикреплено фотоматериалов.", reply_markup=get_portal_keyboard())
    else:
        text = f"📎 <b>Материалы к заданию #{task_id} ({len(materials)} шт.):</b>\n"
        for m in materials:
            text += f"- [{m['file_id']}] {m.get('caption', '')} (Автор: {m.get('uploader_nick', '')})\n"
        await message.answer(text, parse_mode="HTML", reply_markup=get_portal_keyboard())

@dp.message(Command("help"))
async def handle_help(message: Message):
    record_user_activity(message.from_user)
    help_text = (
        "🤖 <b>Команды ДЗ Бота 10-го класса:</b>\n\n"
        "• <b>/start</b> — Главное меню и запуск приложения\n"
        "• <b>/remind</b> — Проверить задания на завтра с кнопкой запуска\n"
        "• <b>/today</b> — Задания на сегодня\n"
        "• <b>/priority</b> — Все невыполненные задания по дням\n"
        "• <b>/overdue</b> — Просроченные задания\n"
        "• <b>/done &lt;id&gt;</b> — Отметить задание выполненным\n\n"
        "💡 <i>Самый удобный способ управлять домашними заданиями, загружать фото и листать страницы — наш интерактивный ДЗ Портал:</i>"
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=get_portal_keyboard())

# --- Text fallback handler ---
# Replaces old text parsing with a friendly guide to the WebApp portal
@dp.message()
async def handle_text_fallback(message: Message):
    record_user_activity(message.from_user)
    user_name = message.from_user.first_name or "ученик"
    text = (
        f"👋 <b>{html.escape(user_name)}</b>, для просмотра и добавления домашних заданий используйте наш <b>ДЗ Портал</b>!\n\n"
        f"В приложении вы можете:\n"
        f"• 📚 Смотреть все предметы и дедлайны\n"
        f"• 📸 Загружать и увеличивать фото заданий\n"
        f"• ✅ Отмечать сделанное в один клик\n"
        f"• ➕ Быстро публиковать новые задания\n\n"
        f"Нажмите кнопку ниже, чтобы открыть портал 👇"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_portal_keyboard("🚀 Открыть ДЗ Портал"))

async def main():
    if not bot:
        logger.error("❌ BOT_TOKEN is missing! Please configure BOT_TOKEN in .env or Render Environment Variables.")
        return
    init_db()
    # 1. Start background reminder scheduler (every day at 18:00)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_evening_reminder, 'cron', hour=18, minute=0)
    scheduler.start()
    logger.info("⏰ Background scheduler started (evening reminder at 18:00)")

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
