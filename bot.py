import os
import asyncio
import logging
import html
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
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
    get_all_users,
    get_notification_prefs,
    upsert_notification_prefs
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Loads secret environment variables
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEB_APP_URL = (
    os.getenv('WEB_APP_URL')
    or os.getenv('RENDER_EXTERNAL_URL')
    or (f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}" if os.getenv('RENDER_EXTERNAL_HOSTNAME') else None)
    or 'https://hw-manager-portal.onrender.com'
).rstrip('/')

TARGET_TZ_NAME = os.getenv("BOT_TIMEZONE", "Asia/Tashkent")
try:
    TARGET_TZ = ZoneInfo(TARGET_TZ_NAME)
except Exception:
    TARGET_TZ = ZoneInfo("Asia/Tashkent")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

async def sync_menu_button(chat_id: int = None):
    """
    Synchronizes Telegram Chat Menu Button (the button next to the message field)
    to the permanent active WebApp URL.
    """
    if not bot:
        return
    try:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(
                text="🎒 ДЗ Портал",
                web_app=WebAppInfo(url=WEB_APP_URL)
            )
        )
        logger.info(f"✅ Telegram Chat Menu Button synced to: {WEB_APP_URL} (chat_id={chat_id})")
    except Exception as e:
        logger.warning(f"Note syncing menu button: {e}")

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

# Точные короткие аббревиатуры из расписания
EXACT_SHORT_ICONS = {
    'ая': '🇬🇧',
    'уя': '🇺🇿',
    'пд': '📑',
    'шу': '🎓',
    'ндп': '🛡️',
    'ивт': '💻',
    'рил': '📚',
    'мат': '📐',
    'физ': '⚡',
    'ист': '🏛️',
    'ф-ра': '⚽',
    'ор.иск': '🎭',
    'ор.иск.': '🎭'
}

# Упорядоченный список сопоставления ключевых слов (от более специфичных к общим)
SUBJECT_ORDERED_KEYWORDS = [
    ('физкульт', '⚽'),
    ('физ-р', '⚽'),
    ('ф-ра', '⚽'),
    ('проект', '📑'),
    ('оратор', '🎭'),
    ('ор.иск', '🎭'),
    ('театр', '🎭'),
    ('рил', '📚'),
    ('литератур', '📚'),
    ('русск', '📚'),
    ('узбек', '🇺🇿'),
    ('геометр', '📐'),
    ('алгебр', '📐'),
    ('матем', '📐'),
    ('информ', '💻'),
    ('ивт', '💻'),
    ('истори', '🏛️'),
    ('ист', '🏛️'),
    ('биолог', '🧬'),
    ('географ', '🌍'),
    ('хими', '🧪'),
    ('обществ', '⚖️'),
    ('обж', '🦺'),
    ('шу', '🎓'),
    ('ндп', '🛡️'),
    ('физик', '⚡'),
    ('физ', '⚡'),
    ('англ', '🇬🇧'),
    ('english', '🇬🇧'),
    ('math', '📐'),
    ('cs', '💻'),
    ('мат', '📐'),
]

def get_subject_icon(subject: str) -> str:
    s = (subject or "").strip().lower()
    s_clean = s.rstrip('.')
    if s in EXACT_SHORT_ICONS:
        return EXACT_SHORT_ICONS[s]
    if s_clean in EXACT_SHORT_ICONS:
        return EXACT_SHORT_ICONS[s_clean]

    for key, icon in SUBJECT_ORDERED_KEYWORDS:
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

def get_local_today() -> date:
    """Returns today's date in target timezone (Asia/Tashkent)."""
    return datetime.now(TARGET_TZ).date()

def build_reminder_notification(user_id: int, hour: Optional[int] = None) -> tuple[str, bool]:
    """
    Builds contextual homework notification text for the user depending on the hour (08:00, 16:00, 18:00).
    Returns (formatted_html_text, has_tasks).
    """
    now_local = datetime.now(TARGET_TZ)
    target_hour = hour if hour is not None else now_local.hour

    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    today_str = today.isoformat()
    tomorrow_str = tomorrow.isoformat()

    pending = get_pending_tasks(user_id)
    today_tasks = [t for t in pending if str(t.get('due_date')) == today_str]
    tomorrow_tasks = [t for t in pending if str(t.get('due_date')) == tomorrow_str]
    overdue_tasks = [t for t in pending if str(t.get('due_date')) < today_str]

    today_nom = RU_WEEKDAYS_NOMINATIVE.get(today.weekday(), "Сегодня")
    today_label = f"{today.day} {RU_MONTHS[today.month]}"
    tomorrow_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
    tomorrow_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"
    tomorrow_prep = RU_WEEKDAYS.get(tomorrow.weekday(), "завтра")

    def format_task_list(tasks):
        lines = []
        for idx, t in enumerate(tasks, 1):
            icon = get_subject_icon(t.get('subject', ''))
            subj = html.escape(str(t.get('subject', 'Предмет')).strip())
            desc = html.escape(str(t.get('description', '')).strip())
            lines.append(f"  {idx}. {icon} <b>{subj}</b>: {desc}")
        return lines

    if target_hour <= 10:
        # --- 08:00 Morning Reminder ---
        has_tasks = bool(today_tasks or tomorrow_tasks or overdue_tasks)
        if not has_tasks:
            text = (
                f"☀️ <b>Доброе утро!</b>\n\n"
                f"📅 <i>{today_nom}, {today_label}</i>\n\n"
                f"🎉 На сегодня и завтра нет невыполненных заданий!\n"
                f"Отличного и продуктивного учебного дня! 🚀"
            )
            return text, False

        lines = [
            f"☀️ <b>Доброе утро! Напоминание о домашнем задании:</b>",
            f"📅 <b>{today_nom}, {today_label}</b>",
            ""
        ]
        if today_tasks:
            lines.append(f"🔴 <b>Сдать сегодня ({len(today_tasks)}):</b>")
            lines.extend(format_task_list(today_tasks))
            lines.append("")

        if tomorrow_tasks:
            lines.append(f"📚 <b>Задания на завтра ({len(tomorrow_tasks)}):</b>")
            lines.extend(format_task_list(tomorrow_tasks))
            lines.append("")

        if overdue_tasks:
            lines.append(f"⚠️ <b>Просроченные задания ({len(overdue_tasks)}):</b>")
            lines.extend(format_task_list(overdue_tasks))
            lines.append("")

        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, посмотреть страницы учебников или отметить сделанным 👇")
        return "\n".join(lines).strip(), True

    elif target_hour <= 16:
        # --- 16:00 Afternoon / After-school Reminder ---
        has_tasks = bool(tomorrow_tasks or today_tasks or overdue_tasks)
        if not has_tasks:
            text = (
                f"🎒 <b>Дневное напоминание о ДЗ</b>\n\n"
                f"📅 <i>{tomorrow_nom}, {tomorrow_label} (завтра)</i>\n\n"
                f"🎉 На завтра нет невыполненных заданий!\n"
                f"Все уроки готовы, можно отдыхать или заняться своими делами ☕"
            )
            return text, False

        lines = [
            f"🎒 <b>Дневное напоминание о домашнем задании!</b>",
            f"📅 <b>{tomorrow_nom}, {tomorrow_label} (завтра)</b>",
            ""
        ]
        if tomorrow_tasks:
            lines.append(f"📚 <b>Задания на завтра ({len(tomorrow_tasks)}):</b>")
            lines.extend(format_task_list(tomorrow_tasks))
            lines.append("")

        if today_tasks:
            lines.append(f"⏳ <b>Осталось сдать на сегодня ({len(today_tasks)}):</b>")
            lines.extend(format_task_list(today_tasks))
            lines.append("")

        if overdue_tasks:
            lines.append(f"⚠️ <b>Просроченные долги ({len(overdue_tasks)}):</b>")
            lines.extend(format_task_list(overdue_tasks))
            lines.append("")

        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, сверить фото заданий и отметить сделанным 👇")
        return "\n".join(lines).strip(), True

    else:
        # --- 18:00 Evening Reminder ---
        remaining = today_tasks + overdue_tasks
        has_tasks = bool(tomorrow_tasks or remaining)
        if not has_tasks:
            text = (
                f"🌆 <b>Вечерний чек-лист ДЗ</b>\n\n"
                f"📅 <i>{tomorrow_nom}, {tomorrow_label} (завтра)</i>\n\n"
                f"🎉 На завтра нет невыполненных заданий!\n"
                f"Все уроки сделаны, приятного вечера и хорошего отдыха ✨"
            )
            return text, False

        lines = [
            f"🌆 <b>Вечерняя проверка домашнего задания {tomorrow_prep}!</b>",
            f"📅 <b>{tomorrow_nom}, {tomorrow_label}</b>",
            ""
        ]
        if tomorrow_tasks:
            lines.append(f"📚 <b>Задания на завтра ({len(tomorrow_tasks)}):</b>")
            lines.extend(format_task_list(tomorrow_tasks))
            lines.append("")

        if remaining:
            lines.append(f"⚠️ <b>Не сданные сегодня или ранее ({len(remaining)}):</b>")
            lines.extend(format_task_list(remaining))
            lines.append("")

        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, просмотреть прикреплённые фото или отметить сделанным 👇")
        return "\n".join(lines).strip(), True

def build_tomorrow_notification(user_id: int) -> tuple[str, bool]:
    """
    Builds tomorrow's homework notification text and returns (formatted_text, has_tasks).
    Backward-compatible alias for build_reminder_notification(user_id, hour=18).
    """
    return build_reminder_notification(user_id, hour=18)

async def send_scheduled_reminder(target_hour: Optional[int] = None):
    """
    Scheduled job sending targeted homework notifications to all registered students
    at 08:00, 16:00, and 18:00 local time (Asia/Tashkent).
    """
    if not bot:
        logger.warning("⏰ send_scheduled_reminder skipped: Telegram Bot instance is not initialized")
        return

    now_local = datetime.now(TARGET_TZ)
    current_hour = target_hour if target_hour is not None else now_local.hour
    logger.info(f"⏰ Running scheduled reminder for hour={current_hour} (local time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')})...")

    users = get_all_users()
    if not users:
        # Fallback to configured user ID
        users = [{"id": 5783100321}]

    sent_count = 0
    skipped_count = 0
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        try:
            # Check personal notification preferences
            prefs = get_notification_prefs(uid)
            pref_key = f"notify_{current_hour:02d}"
            if not prefs.get(pref_key, True):
                skipped_count += 1
                logger.info(f"⏭ Skipping user {uid}: {pref_key} is disabled")
                continue

            text, has_tasks = build_reminder_notification(uid, hour=current_hour)
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
            logger.warning(f"Could not send reminder (hour={current_hour}) to user {uid}: {e}")

    logger.info(f"⏰ Scheduled reminder for hour={current_hour} finished: sent {sent_count} notifications, {skipped_count} skipped (no tasks).")

async def send_evening_reminder():
    """Backward-compatible wrapper calling send_scheduled_reminder for 18:00."""
    await send_scheduled_reminder(target_hour=18)

# ─────────────────────────────────────────────────────────────
# NOTIFICATION SETTINGS HELPERS
# ─────────────────────────────────────────────────────────────

def build_settings_text(prefs: dict) -> str:
    def st(enabled: bool) -> str:
        return "ВКЛ ✅" if enabled else "ВЫКЛ ❌"
    return (
        "⚙️ <b>Настройки уведомлений</b>\n\n"
        "Выбери, в какое время получать напоминания о домашних заданиях:\n\n"
        f"☀️ <b>08:00</b> — Утреннее   [{st(prefs.get('notify_08', True))}]\n"
        f"🎒 <b>16:00</b> — Дневное    [{st(prefs.get('notify_16', True))}]\n"
        f"🌆 <b>18:00</b> — Вечернее   [{st(prefs.get('notify_18', True))}]\n\n"
        "<i>Нажми на нужное время, чтобы включить или выключить.</i>"
    )

def build_settings_keyboard(prefs: dict) -> InlineKeyboardMarkup:
    def label(hour_key: str, emoji: str, time: str) -> str:
        on = prefs.get(hour_key, True)
        return f"{emoji} {time} {'✅' if on else '❌'}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label("notify_08", "☀️", "08:00"), callback_data="toggle_notif:08")],
        [InlineKeyboardButton(text=label("notify_16", "🎒", "16:00"), callback_data="toggle_notif:16")],
        [InlineKeyboardButton(text=label("notify_18", "🌆", "18:00"), callback_data="toggle_notif:18")],
    ])

@dp.message(Command("settings", "уведомления", "notif"))
async def handle_settings(message: Message):
    record_user_activity(message.from_user)
    prefs = get_notification_prefs(message.from_user.id)
    await message.answer(
        build_settings_text(prefs),
        parse_mode="HTML",
        reply_markup=build_settings_keyboard(prefs)
    )

@dp.callback_query()
async def handle_done_callback(callback: CallbackQuery):
    # ── Toggle notification preference ──────────────────────
    if callback.data.startswith("toggle_notif:"):
        hour = callback.data.split(":")[1]  # "08", "16", "18"
        uid = callback.from_user.id
        prefs = get_notification_prefs(uid)
        key = f"notify_{hour}"
        prefs[key] = not prefs.get(key, True)
        upsert_notification_prefs(uid, prefs["notify_08"], prefs["notify_16"], prefs["notify_18"])
        await callback.answer("✅ Сохранено!")
        # Edit message to reflect new state
        await callback.message.edit_text(
            build_settings_text(prefs),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(prefs)
        )
        return

    if callback.data == "open_settings":
        prefs = get_notification_prefs(callback.from_user.id)
        await callback.answer()
        await callback.message.answer(
            build_settings_text(prefs),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(prefs)
        )
        return

    if callback.data.startswith("done:"):
        task_id = int(callback.data.split(":")[1])
        mark_task_done(task_id, callback.from_user.id)
        await callback.answer("Задание выполнено! 🎉")
        await callback.message.edit_text(f"Задание #{task_id} выполнено! ✅")

@dp.message(CommandStart())
async def handle_start(message: Message):
    record_user_activity(message.from_user)
    try:
        await sync_menu_button(message.chat.id)
    except Exception:
        pass
    user_name = message.from_user.first_name or "ученик"
    text = (
        f"👋 Привет, <b>{html.escape(user_name)}</b>!\n\n"
        f"🎒 Добро пожаловать в <b>ДЗ Портал 10-го класса</b>.\n\n"
        f"Здесь собраны все актуальные домашние задания, дедлайны, фото учебников и конспектов.\n\n"
        f"Нажмите кнопку ниже, чтобы открыть приложение 👇"
    )
    start_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть ДЗ Портал", web_app=WebAppInfo(url=WEB_APP_URL))],
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="open_settings")]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=start_keyboard)

@dp.message(Command("remind", "notify", "tomorrow", "zavtra"))
async def handle_remind(message: Message, command: CommandObject = None):
    """
    On-demand command for testing and checking homework notifications.
    Supports optional hour argument: /remind, /remind 8, /remind 16, /remind 18
    """
    record_user_activity(message.from_user)
    target_hour = None
    if command and command.args:
        arg = command.args.strip().lower()
        if arg in ("8", "08", "morning", "утро"):
            target_hour = 8
        elif arg in ("16", "day", "afternoon", "день"):
            target_hour = 16
        elif arg in ("18", "evening", "вечер"):
            target_hour = 18
        elif arg.isdigit():
            target_hour = int(arg)

    text, has_tasks = build_reminder_notification(message.from_user.id, hour=target_hour)
    btn_text = "🚀 Открыть ДЗ Портал" if has_tasks else "🎒 Открыть ДЗ Портал"
    await message.answer(text, parse_mode="HTML", reply_markup=get_portal_keyboard(btn_text))

@dp.message(Command("notify_all", "broadcast_reminders"))
async def handle_notify_all(message: Message, command: CommandObject = None):
    """
    Admin command to immediately broadcast notifications to all students.
    Supports optional hour argument: /notify_all 8, /notify_all 16, /notify_all 18
    """
    record_user_activity(message.from_user)
    target_hour = None
    if command and command.args:
        arg = command.args.strip().lower()
        if arg in ("8", "08", "morning", "утро"):
            target_hour = 8
        elif arg in ("16", "day", "afternoon", "день"):
            target_hour = 16
        elif arg in ("18", "evening", "вечер"):
            target_hour = 18
        elif arg.isdigit():
            target_hour = int(arg)

    mode_label = f"{target_hour}:00" if target_hour is not None else "текущее время"
    await message.answer(f"📢 Запускаю рассылку напоминаний ({mode_label}) всем ученикам...")
    users = get_all_users()
    if not users:
        users = [{"id": message.from_user.id}]

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        try:
            text, has_tasks = build_reminder_notification(uid, hour=target_hour)
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
        f"✅ <b>Рассылка ({mode_label}) завершена!</b>\n\n"
        f"• Отправлено: <b>{sent_count}</b>\n"
        f"• Без заданий (пропущено): <b>{skipped_count}</b>\n"
        f"• Ошибок отправки: <b>{failed_count}</b>",
        parse_mode="HTML",
        reply_markup=get_portal_keyboard()
    )

@dp.message(Command("today"))
async def handle_today_tasks(message: Message):
    record_user_activity(message.from_user)
    today = get_local_today()
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
    today_str = get_local_today().isoformat()
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
        "• <b>/settings</b> — Настройки личных уведомлений (08:00, 16:00, 18:00)\n"
        "• <b>/remind [8|16|18]</b> — Проверить уведомления (утро, день, вечер)\n"
        "• <b>/notify_all [8|16|18]</b> — Разослать напоминания всем ученикам\n"
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
    # 1. Start background reminder scheduler (every day at 08:00, 16:00, 18:00 in Asia/Tashkent)
    scheduler = AsyncIOScheduler(timezone=TARGET_TZ)
    scheduler.add_job(send_scheduled_reminder, 'cron', hour='8,16,18', minute=0)
    scheduler.start()
    logger.info(f"⏰ Background scheduler started (reminders at 08:00, 16:00, 18:00 in {TARGET_TZ})")

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
