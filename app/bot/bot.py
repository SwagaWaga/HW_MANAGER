import os
import re
import time
import asyncio
import logging
import html
import base64
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple, Callable, Awaitable
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.filters import CommandStart, Command, CommandObject, ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ChatMemberUpdated, FSInputFile, InputMediaPhoto, TelegramObject,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
)
try:
    from app.database.db import (
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
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        normalize_time_str,
        parse_custom_times,
        upsert_group_chat,
        get_group_chat,
        get_active_group_chats,
        set_group_reminder,
        get_all_subjects,
        get_class_tasks_for_date,
        get_upcoming_tasks_for_subject
    )
except ImportError:
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
        upsert_notification_prefs,
        get_leaderboard,
        cleanup_old_tasks,
        normalize_time_str,
        parse_custom_times,
        upsert_group_chat,
        get_group_chat,
        get_active_group_chats,
        set_group_reminder,
        get_all_subjects,
        get_class_tasks_for_date,
        get_upcoming_tasks_for_subject
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

# Версия приложения для мгновенного сброса кэша Telegram WebView
APP_VERSION = "2.10.0"
WEB_APP_LAUNCH_URL = f"{WEB_APP_URL}?v={APP_VERSION}"

TARGET_TZ_NAME = os.getenv("BOT_TIMEZONE", "Asia/Tashkent")
try:
    TARGET_TZ = ZoneInfo(TARGET_TZ_NAME)
except Exception:
    TARGET_TZ = ZoneInfo("Asia/Tashkent")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# ─────────────────────────────────────────────────────────────
# АНТИ-СПАМ И ЗАЩИТА ОТ ФЛУДА (RATE LIMITING & FLOOD CONTROL)
# ─────────────────────────────────────────────────────────────
class AntiSpamMiddleware(BaseMiddleware):
    """
    Интеллектуальная защита от спама и флуда в личных и групповых чатах:
    1. Ограничение частоты запросов (rate limiting / throttling):
       - В личных чатах: не чаще 1 команды в 1.2 секунды.
       - В групповых чатах: не чаще 1 команды в 2.5 секунды от одного пользователя
         (так как отправка Д/З с карточками и фото требует нескольких сообщений Telegram).
       - Общий флуд-контроль группы: не чаще 1 команды в 1.2 секунды на группу в целом.
    2. Поведение при спаме:
       - Первое превышение частоты: вежливое предупреждение с оставшимся временем ожидания (не чаще 1 раза в 8 секунд).
       - Повторный спам (2-4 раза подряд): ПОЛНЫЙ МОЛЧАЛИВЫЙ СБРОС (silent drop). Бот ничего не отправляет и не тратит лимиты.
       - Злостный спам (>= 5 раз подряд): временная теневая блокировка (shadow ban) на 30 секунд.
    3. Защита от спама по inline-кнопкам (CallbackQuery debounce 0.6 сек).
    4. Автоматическая очистка устаревших записей в памяти.
    """
    def __init__(self):
        super().__init__()
        self.user_stats: Dict[int, Dict[str, Any]] = {}
        self.group_last_time: Dict[int, float] = {}
        self.cb_last_time: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        loop = asyncio.get_event_loop()
        now = loop.time()

        # 1. Защита от спама по кнопкам (CallbackQuery)
        if isinstance(event, CallbackQuery):
            user = event.from_user
            if user:
                last_cb = self.cb_last_time.get(user.id, 0.0)
                if (now - last_cb) < 0.6:
                    try:
                        await event.answer("⏳ Подождите...", show_alert=False)
                    except Exception:
                        pass
                    return
                self.cb_last_time[user.id] = now
            return await handler(event, data)

        # 2. Защита от спама по сообщениям (Message)
        if not isinstance(event, Message):
            return await handler(event, data)

        user = event.from_user
        if not user or user.is_bot:
            return await handler(event, data)

        chat = event.chat
        is_group = chat.type in ("group", "supergroup")
        user_id = user.id

        # В групповых чатах проверяем, направлено ли сообщение боту (команда, упоминание, ответ боту или вопрос о ДЗ).
        # Обычные разговоры одноклассников пропускаем без учёта в спам-фильтре, чтобы они не блокировали команды.
        if is_group:
            raw_text = (event.text or event.caption or "").strip()
            is_targeted = False
            if raw_text.startswith("/"):
                is_targeted = True
            elif "@" in raw_text and "hw" in raw_text.lower():
                is_targeted = True
            elif event.reply_to_message and event.reply_to_message.from_user and event.reply_to_message.from_user.is_bot:
                is_targeted = True
            elif any(kw in raw_text.lower() for kw in ("дз", "д/з", "домашка", "задали", "задано", "уроки")):
                is_targeted = True

            if not is_targeted:
                return await handler(event, data)

        # Авто-очистка устаревших записей памяти
        if len(self.user_stats) > 1000:
            cutoff = now - 180.0
            self.user_stats = {
                uid: s for uid, s in self.user_stats.items()
                if s.get("last_time", 0) > cutoff or s.get("banned_until", 0) > now
            }

        stats = self.user_stats.setdefault(user_id, {
            "last_time": 0.0,
            "spam_count": 0,
            "last_warned": 0.0,
            "banned_until": 0.0
        })

        # А) Проверка временного бана за спам
        if stats["banned_until"] > now:
            # Предупреждаем о remaining time только раз в 10 секунд, чтобы не засорять чат
            if (now - stats["last_warned"]) > 10.0:
                stats["last_warned"] = now
                remaining = max(1, int(stats["banned_until"] - now))
                try:
                    await safe_reply(
                        event,
                        f"⛔ <b>Действует временная блокировка от спама.</b>\n"
                        f"Пожалуйста, подождите ещё <b>{remaining} сек.</b> перед повторным запросом."
                    )
                except Exception:
                    pass
            return

        # Б) Определение минимального интервала между командами (1.0 сек для комфортной работы)
        limit_interval = 1.0
        elapsed = now - stats["last_time"]

        # В) Обработка превышения скорости (спама)
        if elapsed < limit_interval:
            stats["spam_count"] += 1
            # ВАЖНО: не сдвигаем stats["last_time"] вперед, чтобы не запирать пользователя в бесконечной блокировке!

            # Злостный спам (5+ раз подряд быстрее 1 секунды)
            if stats["spam_count"] >= 5:
                stats["banned_until"] = now + 25.0
                stats["spam_count"] = 0
                try:
                    await safe_reply(
                        event,
                        "⛔ <b>Слишком много запросов!</b>\n"
                        "Пожалуйста, подождите 25 секунд перед следующей командой."
                    )
                except Exception:
                    pass
                return

            # Предупреждение пользователю (не чаще одного раза в 6 секунд, чтобы не засорять чат)
            if (now - stats["last_warned"]) > 6.0:
                stats["last_warned"] = now
                try:
                    await safe_reply(
                        event,
                        "⏳ <b>Пожалуйста, не спамьте командами!</b>\n"
                        "Подождите секунду перед следующим запросом."
                    )
                except Exception:
                    pass

            # Молча сбрасываем выполнение при спаме
            return

        # Успешный запрос в пределах лимита
        stats["spam_count"] = 0
        stats["last_time"] = now

        return await handler(event, data)


# Подключение Anti-Spam Middleware
anti_spam_middleware = AntiSpamMiddleware()
dp.message.middleware(anti_spam_middleware)
dp.callback_query.middleware(anti_spam_middleware)

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
                text="🚀 ДЗ Портал",
                web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL)
            )
        )
        logger.info(f"✅ Telegram Chat Menu Button synced to: {WEB_APP_URL} (chat_id={chat_id})")
    except Exception as e:
        logger.warning(f"Note syncing menu button: {e}")

async def sync_bot_profile():
    """
    Sets global Menu Button (Mini App launch button in Telegram chat),
    custom short description (replaces the plain 'bot' in chat list),
    full bot description, and bot commands menu.
    """
    if not bot:
        return
    from aiogram.types import MenuButtonWebApp, WebAppInfo, BotCommand

    # 1. Global Menu Button
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🚀 ДЗ Портал",
                web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL)
            )
        )
        logger.info(f"✅ Global Chat Menu Button set to: {WEB_APP_URL}")
    except Exception as e:
        logger.warning(f"Note setting global menu button: {e}")

    # 2. Short description (replaces grey 'bot' subtitle under bot name in chat list)
    try:
        await bot.set_my_short_description(
            "🎒 ДЗ Портал 10-А • Расписание, домашка и конспекты"
        )
        logger.info("✅ Bot short description updated.")
    except Exception as e:
        logger.warning(f"Note setting short description: {e}")

    # 3. Full description (displayed on empty chat screen before clicking Start)
    try:
        await bot.set_my_description(
            "🎒 Официальный бот и WebApp портал 10-А класса.\n\n"
            "✨ Актуальное расписание уроков и звонков\n"
            "📚 Домашние задания и дедлайны по дням\n"
            "📸 Фотографии конспектов, учебников и доски\n"
            "🏆 Таблица лидеров и самых активных редакторов\n"
            "⚙️ Персональные напоминания в 08:00, 16:00, 18:00\n\n"
            "Нажмите «Запустить» или синюю кнопку в чате, чтобы открыть приложение!"
        )
        logger.info("✅ Bot full description updated.")
    except Exception as e:
        logger.warning(f"Note setting full description: {e}")

    # 4. Commands menu (для личных и групповых чатов)
    try:
        private_commands = [
            BotCommand(command="start", description="🚀 Открыть ДЗ Портал 10-А"),
            BotCommand(command="tomorrow", description="📚 Домашнее задание на завтра"),
            BotCommand(command="today", description="📅 Уроки и задания на сегодня"),
            BotCommand(command="week", description="🗓️ Все задания на неделю"),
            BotCommand(command="priority", description="⚡ Срочные задания по дням"),
            BotCommand(command="top", description="🏆 Таблица лидеров редакторов"),
            BotCommand(command="settings", description="⚙️ Настройки уведомлений (8/16/18/2 дня/неделя)"),
            BotCommand(command="overdue", description="⏳ Просроченные задания"),
            BotCommand(command="help", description="ℹ️ Справка по всем командам"),
        ]
        group_commands = [
            BotCommand(command="tomorrow", description="📚 Д/З на завтра (или /tomorrow алгебра)"),
            BotCommand(command="today", description="📅 Д/З на сегодня (или /today физика)"),
            BotCommand(command="remind_group", description="⏰ Напоминания в группе"),
            BotCommand(command="help", description="ℹ️ Справка по командам в группе"),
        ]
        try:
            await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
            await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        except Exception:
            await bot.set_my_commands(private_commands)
        logger.info("✅ Bot commands updated for private and group chats.")
    except Exception as e:
        logger.warning(f"Note setting bot commands: {e}")

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

def get_portal_keyboard(button_text: str = "🚀 Открыть ДЗ Портал", chat_or_type: Any = None) -> InlineKeyboardMarkup:
    """
    Возвращает клавиатуру для открытия ДЗ Портала.
    В личных чатах используется WebAppInfo (открытие Mini App прямо внутри Telegram).
    В групповых чатах Telegram ЗАПРЕЩАЕТ WebAppInfo кнопки (ошибка BUTTON_TYPE_INVALID),
    поэтому для групп создаётся безопасная ссылка на запуск приложения через бота.
    """
    is_group = False
    if isinstance(chat_or_type, str):
        is_group = chat_or_type in ("group", "supergroup", "channel")
    elif hasattr(chat_or_type, "type"):
        is_group = chat_or_type.type in ("group", "supergroup", "channel")
    elif hasattr(chat_or_type, "chat") and hasattr(chat_or_type.chat, "type"):
        is_group = chat_or_type.chat.type in ("group", "supergroup", "channel")

    bot_user = "my_hw_manager_bot"
    if is_group:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=f"https://t.me/{bot_user}?start=portal")
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL))
    ]])

def sanitize_markup_for_chat(reply_markup: Optional[InlineKeyboardMarkup], chat_or_type: Any) -> Optional[InlineKeyboardMarkup]:
    """
    Проверяет разметку кнопок на совместимость с типом чата.
    Telegram в группах/супергруппах/каналах строго запрещает InlineKeyboardButton(web_app=...),
    возвращая ошибку BUTTON_TYPE_INVALID. Данная функция заменяет их на url-ссылку на бота.
    """
    if not reply_markup or not hasattr(reply_markup, "inline_keyboard"):
        return reply_markup

    is_group = False
    if isinstance(chat_or_type, str):
        is_group = chat_or_type in ("group", "supergroup", "channel")
    elif hasattr(chat_or_type, "type"):
        is_group = chat_or_type.type in ("group", "supergroup", "channel")
    elif hasattr(chat_or_type, "chat") and hasattr(chat_or_type.chat, "type"):
        is_group = chat_or_type.chat.type in ("group", "supergroup", "channel")

    if not is_group:
        return reply_markup

    bot_user = "my_hw_manager_bot"
    new_keyboard = []
    has_changes = False
    for row in reply_markup.inline_keyboard:
        new_row = []
        for btn in row:
            if getattr(btn, "web_app", None):
                new_row.append(InlineKeyboardButton(
                    text=btn.text,
                    url=f"https://t.me/{bot_user}?start=portal"
                ))
                has_changes = True
            else:
                new_row.append(btn)
        new_keyboard.append(new_row)

    if has_changes:
        return InlineKeyboardMarkup(inline_keyboard=new_keyboard)
    return reply_markup

async def safe_reply(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML"
):
    """
    Безопасная отправка сообщения.
    Автоматически адаптирует кнопки под тип чата (личный vs группа),
    и в случае любых ограничений Telegram отправляет сообщение без ошибок.
    Никогда не роняет вызывающий хэндлер необработанным исключением.
    """
    if reply_markup is None:
        reply_markup = get_portal_keyboard(chat_or_type=message)
    else:
        reply_markup = sanitize_markup_for_chat(reply_markup, message)
    try:
        return await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Failed to reply with markup in chat {message.chat.id}: {e}")
        try:
            fallback_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎒 Открыть ДЗ Портал", url="https://t.me/my_hw_manager_bot?start=portal")
            ]])
            return await message.answer(text, parse_mode=parse_mode, reply_markup=fallback_kb)
        except Exception as e2:
            try:
                return await message.answer(text, parse_mode=parse_mode)
            except Exception as e3:
                # Фолбэк на случай ошибки парсинга HTML-тегов Telegram
                try:
                    plain = re.sub(r'<[^>]+>', '', text)
                    return await message.answer(plain)
                except Exception as e4:
                    logger.error(f"safe_reply completely failed in chat {message.chat.id}: {e4}")
                    return None

async def safe_send_photo(
    message: Message,
    photo_path: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None
):
    """Безопасная отправка фото с клавиатурой и авто-фолбэком."""
    if reply_markup is None:
        reply_markup = get_portal_keyboard("🚀 Открыть в ДЗ Портале", chat_or_type=message)
    else:
        reply_markup = sanitize_markup_for_chat(reply_markup, message)
    try:
        return await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=caption[:1024],
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Failed to send photo with markup: {e}")
        try:
            return await message.answer_photo(
                photo=FSInputFile(photo_path),
                caption=caption[:1024],
                parse_mode="HTML"
            )
        except Exception as e2:
            try:
                # Если ошибка в HTML caption
                plain = re.sub(r'<[^>]+>', '', caption)
                return await message.answer_photo(
                    photo=FSInputFile(photo_path),
                    caption=plain[:1024]
                )
            except Exception as e3:
                logger.warning(f"Failed to send photo plain: {e3}")
                return await safe_reply(message, caption, reply_markup=reply_markup)

def format_plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение существительных после числительных."""
    mod10 = n % 10
    mod100 = n % 100
    if 11 <= mod100 <= 19:
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many

# ─────────────────────────────────────────────────────────────
# УМНОЕ РАСПОЗНАВАНИЕ ПРЕДМЕТОВ (СИНОНИМЫ И АББРЕВИАТУРЫ)
# ─────────────────────────────────────────────────────────────

SUBJECT_SYNONYMS = {
    "Алгебра": ["алгебр", "алебр", "алг"],
    "Геометрия": ["геометр", "геом"],
    "Математика": ["матем", "математик", "мат"],
    "Физика": ["физик", "физ"],
    "Химия": ["хими", "хим"],
    "Биология": ["биолог", "био"],
    "Информатика": ["информ", "ивт", "cs", "инф"],
    "Русский язык": ["русск", "русский", "рус"],
    "Литература": ["литератур", "лит-ра", "литра", "лит", "рил"],
    "Английский язык": ["англ", "английск", "инглиш", "english", "ая"],
    "История": ["истори", "ист"],
    "География": ["географ", "гео"],
    "Обществознание": ["обществ", "общество", "общ"],
    "Физкультура": ["физкульт", "физ-р", "физра", "ф-ра"],
    "Узбекский язык": ["узбек", "узб", "уя"],
    "Ораторское искусство": ["оратор", "ор.иск", "театр"],
    "Проект": ["проект", "пд"],
    "ОБЖ": ["обж"],
    "НДП": ["ндп"],
    "ШУ": ["шу"]
}

def find_subject_in_text(text: str, class_name: str = "10-А") -> Optional[str]:
    """Находит предмет в тексте запроса ученика с учетом синонимов и базы данных."""
    if not text:
        return None
    raw = text.lower().strip()

    # 1. Сначала проверяем точные совпадения с синонимами
    for canonical, keywords in SUBJECT_SYNONYMS.items():
        for kw in keywords:
            if re.search(r'\b' + re.escape(kw), raw) or kw in raw:
                return canonical

    # 2. Проверяем реальные предметы из базы
    try:
        db_subjects = get_all_subjects(class_name)
        for s in db_subjects:
            s_clean = s.strip().lower()
            if s_clean in raw or (len(s_clean) >= 3 and s_clean[:4] in raw):
                return s
    except Exception:
        pass
    return None

SUBJECT_FAMILIES = {
    "Алгебра": ["Алгебра", "Математика", "Геометрия", "Мат", "Алг", "Геом"],
    "Математика": ["Математика", "Алгебра", "Геометрия", "Мат", "Алг", "Геом"],
    "Геометрия": ["Геометрия", "Алгебра", "Математика", "Мат", "Алг", "Геом"],
    "Русский язык": ["Русский язык", "Литература", "Русский и Литература", "РиЛ", "Рус"],
    "Литература": ["Литература", "Русский язык", "Русский и Литература", "РиЛ", "Лит"],
    "РиЛ": ["РиЛ", "Русский и Литература", "Русский язык", "Литература"],
    "Информатика": ["Информатика", "ИВТ", "Информатика (ИВТ)", "Инф"],
    "ИВТ": ["ИВТ", "Информатика", "Информатика (ИВТ)", "Инф"],
    "Английский язык": ["Английский язык", "Английский", "Ая", "English"],
    "Узбекский язык": ["Узбекский язык", "Узбекский", "Уя"],
    "Проектная деятельность": ["Проектная деятельность", "Проект", "Пд", "ПД"],
    "Проект": ["Проект", "Проектная деятельность", "Пд", "ПД"],
}

def get_subject_family(subject_name: str) -> List[str]:
    """Возвращает список связанных предметов или синонимов для поиска (например, Алгебра + Математика)."""
    if not subject_name:
        return []
    s_clean = subject_name.strip()
    if s_clean in SUBJECT_FAMILIES:
        return SUBJECT_FAMILIES[s_clean]
    for k, v in SUBJECT_FAMILIES.items():
        if k.lower() == s_clean.lower():
            return v
    return [s_clean]

def resolve_material_file(m: Dict[str, Any]) -> Optional[str]:
    """
    Гарантированно возвращает локальный путь к файлу изображения.
    Если файла нет на диске (например, после деплоя контейнера Render),
    извлекает его из базы данных и кэширует в uploads/.
    """
    mat_id = m.get("id")
    if mat_id:
        for ext in (".jpg", ".png", ".jpeg", ".webp"):
            disk_p = f"uploads/cached_{mat_id}{ext}"
            if os.path.isfile(disk_p) and os.path.getsize(disk_p) > 0:
                return disk_p

    # Проверяем пути file_id / url
    raw_f = m.get("file_id") or m.get("url") or ""
    if raw_f and not raw_f.startswith("data:"):
        clean_name = os.path.basename(raw_f)
        candidates = [
            raw_f,
            os.path.join("uploads", raw_f),
            os.path.join("uploads", clean_name),
            os.path.join(os.getcwd(), "uploads", clean_name)
        ]
        for c in candidates:
            if os.path.isfile(c) and os.path.getsize(c) > 0:
                return c

    # Если на диске нет — достаем из БД и сохраняем
    if mat_id:
        try:
            from app.database.db import get_material_by_id
            mat = get_material_by_id(mat_id)
            if mat and mat.get("file_id"):
                fid = mat["file_id"]
                if fid.startswith("data:"):
                    header, b64_str = fid.split(",", 1)
                    data = base64.b64decode(b64_str)
                    ext = ".png" if "png" in header else ".jpg"
                    os.makedirs("uploads", exist_ok=True)
                    disk_p = f"uploads/cached_{mat_id}{ext}"
                    with open(disk_p, "wb") as f:
                        f.write(data)
                    return disk_p
                elif os.path.isfile(fid):
                    return fid
        except Exception as e:
            logger.warning(f"Error resolving material #{mat_id}: {e}")

    return None

async def send_single_task_card(
    message: Message,
    task: Dict[str, Any],
    date_badge: str,
    index: Optional[int] = None,
    total: Optional[int] = None
):
    """
    Отправляет одно домашнее задание отдельно со всеми прикрепленными фотографиями страниц.
    - 1 фото -> фото с описанием и кнопкой портала
    - Несколько фото -> красивый фотоальбом (media group)
    - Без фото -> текстовая карточка с кнопкой портала
    """
    task_subject = str(task.get("subject", "Предмет")).strip()
    icon = get_subject_icon(task_subject)
    desc = str(task.get("description", "")).strip()
    author_name = task.get("author_name") or task.get("author_nick") or "ученик"
    materials = task.get("materials") or []

    link_items = []
    for m in materials:
        m_type = m.get("type", "")
        m_url = m.get("url") or m.get("file_id") or ""
        m_cap = m.get("caption") or ""
        if m_type == "link" or m_url.startswith(("http://", "https://")) or m_cap.startswith("[LINK]"):
            u = m_url if m_url.startswith(("http://", "https://")) else ""
            if not u and m_cap.startswith("[LINK]"):
                raw = m_cap[6:]
                u = raw.split("::", 1)[0].strip() if "::" in raw else raw.strip()
            title = m.get("title") or m_cap
            if title.startswith("[LINK]"):
                raw = title[6:]
                title = raw.split("::", 1)[1].strip() if "::" in raw else raw.strip()
            title = title or "Открыть материал"
            if u:
                link_items.append({"url": u, "title": title})

    idx_str = f" [{index}/{total}]" if index and total and total > 1 else ""
    caption_lines = [
        f"{icon} <b>{html.escape(task_subject)}</b>{idx_str}",
        f"{date_badge}",
        f"\n📝 <b>Задание:</b>\n{html.escape(desc) if desc else '<i>(см. прикрепленные фото/ссылки)</i>'}",
    ]

    if link_items:
        caption_lines.append("\n🔗 <b>Материалы и ссылки:</b>")
        for l in link_items:
            u_clean = html.escape(l["url"])
            t_clean = html.escape(l["title"])
            caption_lines.append(f"• <a href=\"{u_clean}\">{t_clean}</a>")

    if author_name:
        caption_lines.append(f"\n👤 <i>Опубликовал: {html.escape(str(author_name))}</i>")

    caption = "\n".join(caption_lines)

    photo_paths = []
    for m in materials:
        p = resolve_material_file(m)
        if p and p not in photo_paths:
            photo_paths.append(p)

    # Формируем клавиатуру с кнопками ссылок + кнопка открытия портала
    base_kb = get_portal_keyboard("🚀 Открыть в ДЗ Портале", chat_or_type=message)
    keyboard_rows = []
    for l in link_items[:3]:
        u = l["url"]
        t = l["title"]
        l_icon = "🎬 " if any(x in u.lower() for x in ("youtube", "youtu.be", "rutube", "vk.com/video")) else ("📄 " if any(x in u.lower() for x in ("docs.google", "drive.google", ".pdf")) else "🔗 ")
        keyboard_rows.append([InlineKeyboardButton(text=f"{l_icon}{t[:28]}", url=u)])
    if base_kb and base_kb.inline_keyboard:
        keyboard_rows.extend(base_kb.inline_keyboard)
    card_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    sent = False
    if photo_paths:
        try:
            if len(photo_paths) == 1:
                await safe_send_photo(
                    message=message,
                    photo_path=photo_paths[0],
                    caption=caption,
                    reply_markup=card_markup
                )
                sent = True
            else:
                media = [
                    InputMediaPhoto(
                        media=FSInputFile(p),
                        caption=caption[:1024] if idx == 0 else None,
                        parse_mode="HTML"
                    )
                    for idx, p in enumerate(photo_paths[:10])
                ]
                await message.answer_media_group(media=media)
                if card_markup and card_markup.inline_keyboard:
                    await message.answer("👇 <b>Действия и материалы:</b>", reply_markup=card_markup, parse_mode="HTML")
                sent = True
        except Exception as err:
            logger.warning(f"Error sending photos for task #{task.get('id')}: {err}")
            sent = False

    if not sent:
        await safe_reply(message, caption, reply_markup=card_markup)

async def send_subject_homework(
    message: Message,
    subject_query: str,
    target_date_str: Optional[str] = None
):
    """
    Находит и отправляет ВСЕ домашние задания по предмету (или семье предметов)
    каждое ОТДЕЛЬНО с фотографиями страниц.
    """
    now_local = datetime.now(TARGET_TZ)
    today = now_local.date()
    today_str = today.isoformat()
    tomorrow = today + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()

    if not target_date_str:
        target_date_str = tomorrow_str

    matched_subject = find_subject_in_text(subject_query) or subject_query.strip()
    subject_family = get_subject_family(matched_subject)

    # 1. Поиск заданий на целевую дату (с учетом родственных предметов, например Алгебра + Математика)
    tasks = get_class_tasks_for_date(target_date_str, class_name="10-А", subject=subject_family)

    # 2. Если на целевую дату нет — ищем ближайшие будущие задания по предмету/семье
    if not tasks:
        upcoming = get_upcoming_tasks_for_subject(subject_family, start_date_str=today_str, class_name="10-А", limit=5)
        if upcoming:
            tasks = upcoming

    # 3. Если всё равно не найдено — ищем любые последние задания по предмету
    if not tasks:
        upcoming_all = get_upcoming_tasks_for_subject(subject_family, start_date_str="2026-01-01", class_name="10-А", limit=5)
        if upcoming_all:
            tasks = upcoming_all

    if not tasks:
        icon = get_subject_icon(matched_subject)
        d_label = "на завтра" if target_date_str == tomorrow_str else f"на {target_date_str}"
        not_found_text = (
            f"{icon} <b>По предмету «{html.escape(matched_subject)}» заданий {d_label} нет!</b>\n\n"
            f"В базе данных пока не найдено актуального Д/З. Возможно, его ещё не записали или уроков нет."
        )
        btn = get_portal_keyboard("🎒 Проверить в ДЗ Портале", chat_or_type=message)
        await safe_reply(message, not_found_text, reply_markup=btn)
        return

    # Если найдено несколько заданий (например, Алгебра и Математика) — краткий заголовок
    if len(tasks) > 1:
        subjects_found = list(dict.fromkeys([str(t.get('subject')).strip() for t in tasks if t.get('subject')]))
        subjs_label = " / ".join(subjects_found)
        await safe_reply(
            message,
            f"📚 <b>Найдено {len(tasks)} {format_plural_ru(len(tasks), 'задание', 'задания', 'заданий')} ({html.escape(subjs_label)}):</b>\n"
            f"<i>Отправляю каждое задание отдельно с фотографиями страниц 👇</i>"
        )

    # Отправляем КАЖДОЕ задание отдельно со всеми фотографиями!
    for idx, task in enumerate(tasks, start=1):
        due_date = str(task.get("due_date", ""))
        if due_date == today_str:
            date_badge = "📅 <b>Сегодня</b>"
        elif due_date == tomorrow_str:
            tomorrow_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
            tomorrow_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"
            date_badge = f"📅 <b>Завтра</b> ({tomorrow_nom}, {tomorrow_label})"
        else:
            try:
                d_obj = datetime.strptime(due_date, "%Y-%m-%d").date()
                d_nom = RU_WEEKDAYS_NOMINATIVE.get(d_obj.weekday(), "")
                d_label = f"{d_obj.day} {RU_MONTHS[d_obj.month]}"
                date_badge = f"📅 <b>{due_date}</b> ({d_nom}, {d_label})"
            except Exception:
                date_badge = f"📅 <b>{due_date}</b>"

        await send_single_task_card(
            message=message,
            task=task,
            date_badge=date_badge,
            index=idx,
            total=len(tasks)
        )
        await asyncio.sleep(0.3)

async def send_group_daily_reminder(group: Dict[str, Any]):
    """Отправляет вечернее напоминание с Д/З на завтра в групповой чат."""
    chat_id = group.get("chat_id")
    if not chat_id or not bot:
        return

    now_local = datetime.now(TARGET_TZ)
    tomorrow = now_local.date() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    tomorrow_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
    tomorrow_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"
    tomorrow_prep = RU_WEEKDAYS.get(tomorrow.weekday(), "завтра")
    class_name = group.get("class_name", "10-А")

    tasks = get_class_tasks_for_date(tomorrow_str, class_name=class_name)

    if not tasks:
        text = (
            f"🌆 <b>Вечерняя проверка домашнего задания {tomorrow_prep}!</b>\n"
            f"📅 <b>{tomorrow_nom}, {tomorrow_label} (завтра)</b>\n\n"
            f"🎉 <b>На завтра уроков не задано!</b>\n"
            f"Отдыхайте или проверьте расписание в приложении."
        )
    else:
        lines = [
            f"🌆 <b>Вечернее напоминание для {class_name}!</b>",
            f"📅 <b>{tomorrow_nom}, {tomorrow_label} (завтра)</b>",
            f"\n📚 <b>Задания на завтра ({len(tasks)}):</b>"
        ]
        for idx, t in enumerate(tasks, start=1):
            subj = str(t.get('subject', 'Предмет')).strip()
            icon = get_subject_icon(subj)
            desc = str(t.get('description', '')).strip()
            mats = t.get('materials') or []
            mat_badge = f" [📷 {len(mats)}]" if mats else ""
            lines.append(f"{idx}. {icon} <b>{html.escape(subj)}</b>{mat_badge}: {html.escape(desc)}")

        lines.append("\n💡 <i>Спросите: «Что по алгебре?» или отправьте <code>/dz [предмет]</code>, чтобы посмотреть фото страниц.</i>")
        text = "\n".join(lines)

    btn = get_portal_keyboard("🚀 Открыть ДЗ Портал", chat_or_type="group")
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=btn)
        logger.info(f"✅ Group reminder sent to {chat_id} ({group.get('title')})")
    except Exception as e:
        logger.warning(f"Could not send group reminder to {chat_id} with markup: {e}")
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass

async def send_group_weekly_reminder(group: Dict[str, Any]):
    """Отправляет воскресный недельный дайджест заданий в групповой чат."""
    chat_id = group.get("chat_id")
    if not chat_id or not bot:
        return
    class_name = group.get("class_name", "10-А")
    now_local = datetime.now(TARGET_TZ)
    monday = now_local.date() + timedelta(days=1)
    sunday = monday + timedelta(days=6)

    days_tasks = {}
    total_count = 0
    for day_offset in range(7):
        curr_d = monday + timedelta(days=day_offset)
        curr_str = curr_d.isoformat()
        t_list = get_class_tasks_for_date(curr_str, class_name=class_name)
        if t_list:
            days_tasks[curr_d] = t_list
            total_count += len(t_list)

    if total_count == 0:
        return

    lines = [
        f"🗓️ <b>План домашнего задания на неделю для {class_name}!</b>",
        f"📅 <b>{monday.day} {RU_MONTHS[monday.month]} – {sunday.day} {RU_MONTHS[sunday.month]}</b>\n"
    ]

    for d_obj, t_list in sorted(days_tasks.items(), key=lambda x: x[0]):
        d_nom = RU_WEEKDAYS_NOMINATIVE.get(d_obj.weekday(), "")
        d_label = f"{d_obj.day} {RU_MONTHS[d_obj.month]}"
        lines.append(f"📌 <b>{d_nom}, {d_label}:</b>")
        for t in t_list:
            subj = str(t.get('subject', 'Предмет')).strip()
            icon = get_subject_icon(subj)
            desc = str(t.get('description', '')).strip()
            mats = t.get('materials') or []
            mat_badge = f" [📷 {len(mats)}]" if mats else ""
            lines.append(f"  • {icon} <b>{html.escape(subj)}</b>{mat_badge}: {html.escape(desc)}")
        lines.append("")

    lines.append("🚀 <i>Открывайте ДЗ Портал для просмотра фото страниц и расписания:</i>")
    btn = get_portal_keyboard("🗓️ Открыть Портал на неделю", chat_or_type="group")
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=btn
        )
        logger.info(f"✅ Group weekly reminder sent to {chat_id}")
    except Exception as e:
        logger.warning(f"Could not send group weekly reminder to {chat_id} with markup: {e}")
        try:
            await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
        except Exception:
            pass

_user_activity_cache: Dict[int, float] = {}

def record_user_activity(from_user):
    if not from_user:
        return
    now = time.time()
    last_sync = _user_activity_cache.get(from_user.id, 0.0)
    # Если пользователя уже синхронизировали в последние 10 минут — не спамим запросами к базе
    if (now - last_sync) < 600.0:
        return
    _user_activity_cache[from_user.id] = now

    nick = f"@{from_user.username}" if from_user.username else (from_user.first_name or "Ученик")
    full_name = f"{from_user.first_name or ''} {from_user.last_name or ''}".strip()

    def _sync_worker():
        try:
            upsert_user(from_user.id, telegram_nick=nick, full_name=full_name)
            from app.services.sync_service import sync_global_user
            sync_global_user(from_user.id, telegram_nick=nick, full_name=full_name, source="telegram_bot")
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _sync_worker)
    except Exception:
        _sync_worker()

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
    in_2_days = today + timedelta(days=2)
    today_str = today.isoformat()
    tomorrow_str = tomorrow.isoformat()
    in_2_days_str = in_2_days.isoformat()

    pending = get_pending_tasks(user_id)
    today_tasks = [t for t in pending if str(t.get('due_date')) == today_str]
    tomorrow_tasks = [t for t in pending if str(t.get('due_date')) == tomorrow_str]
    in_2_days_tasks = [t for t in pending if str(t.get('due_date')) == in_2_days_str]
    overdue_tasks = [t for t in pending if str(t.get('due_date')) < today_str]

    today_nom = RU_WEEKDAYS_NOMINATIVE.get(today.weekday(), "Сегодня")
    today_label = f"{today.day} {RU_MONTHS[today.month]}"
    tomorrow_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
    tomorrow_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"
    tomorrow_prep = RU_WEEKDAYS.get(tomorrow.weekday(), "завтра")
    in_2_days_nom = RU_WEEKDAYS_NOMINATIVE.get(in_2_days.weekday(), "Послезавтра")
    in_2_days_label = f"{in_2_days.day} {RU_MONTHS[in_2_days.month]}"
    in_2_days_prep = RU_WEEKDAYS.get(in_2_days.weekday(), "через 2 дня")

    prefs = get_notification_prefs(user_id)
    include_today = prefs.get('notify_today', True)
    include_tomorrow = prefs.get('notify_tomorrow', True)
    include_2days = prefs.get('notify_2days', True)
    include_overdue = prefs.get('notify_overdue', True)

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
        active_today = today_tasks if include_today else []
        active_tomorrow = tomorrow_tasks if include_tomorrow else []
        active_2days = in_2_days_tasks if include_2days else []
        active_overdue = overdue_tasks if include_overdue else []
        has_tasks = bool(active_today or active_tomorrow or active_2days or active_overdue)
        if not has_tasks:
            text = (
                f"☀️ <b>Доброе утро!</b>\n\n"
                f"📅 <i>{today_nom}, {today_label}</i>\n\n"
                f"🎉 По выбранным настройкам нет невыполненных заданий!\n"
                f"Отличного и продуктивного учебного дня! 🚀"
            )
            return text, False

        lines = [
            f"☀️ <b>Доброе утро! Напоминание о домашнем задании:</b>",
            f"📅 <b>{today_nom}, {today_label}</b>",
            ""
        ]
        if active_today:
            lines.append(f"🔴 <b>Сдать сегодня ({len(active_today)}):</b>")
            lines.extend(format_task_list(active_today))
            lines.append("")

        if active_tomorrow:
            lines.append(f"📚 <b>Задания на завтра ({len(active_tomorrow)}):</b>")
            lines.extend(format_task_list(active_tomorrow))
            lines.append("")

        if active_2days:
            lines.append(f"⏳ <b>Дедлайн через 2 дня ({in_2_days_prep}, {in_2_days_label}) ({len(active_2days)}):</b>")
            lines.extend(format_task_list(active_2days))
            lines.append("")

        if active_overdue:
            lines.append(f"⚠️ <b>Просроченные задания ({len(active_overdue)}):</b>")
            lines.extend(format_task_list(active_overdue))
            lines.append("")

        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, посмотреть страницы учебников или отметить сделанным 👇")
        return "\n".join(lines).strip(), True

    elif target_hour <= 16:
        # --- 16:00 Afternoon / After-school Reminder ---
        active_today = today_tasks if include_today else []
        active_tomorrow = tomorrow_tasks if include_tomorrow else []
        active_2days = in_2_days_tasks if include_2days else []
        active_overdue = overdue_tasks if include_overdue else []
        has_tasks = bool(active_tomorrow or active_2days or active_today or active_overdue)
        if not has_tasks:
            text = (
                f"🎒 <b>Дневное напоминание о ДЗ</b>\n\n"
                f"📅 <i>{tomorrow_nom}, {tomorrow_label} (завтра)</i>\n\n"
                f"🎉 По выбранным настройкам нет невыполненных заданий!\n"
                f"Все уроки готовы, можно отдыхать или заняться своими делами ☕"
            )
            return text, False

        lines = [
            f"🎒 <b>Дневное напоминание о домашнем задании!</b>",
            f"📅 <b>{tomorrow_nom}, {tomorrow_label} (завтра)</b>",
            ""
        ]
        if active_tomorrow:
            lines.append(f"📚 <b>Задания на завтра ({len(active_tomorrow)}):</b>")
            lines.extend(format_task_list(active_tomorrow))
            lines.append("")

        if active_2days:
            lines.append(f"⏳ <b>Дедлайн через 2 дня ({in_2_days_prep}, {in_2_days_label}) ({len(active_2days)}):</b>")
            lines.extend(format_task_list(active_2days))
            lines.append("")

        if active_today:
            lines.append(f"⏳ <b>Осталось сдать на сегодня ({len(active_today)}):</b>")
            lines.extend(format_task_list(active_today))
            lines.append("")

        if active_overdue:
            lines.append(f"⚠️ <b>Просроченные долги ({len(active_overdue)}):</b>")
            lines.extend(format_task_list(active_overdue))
            lines.append("")

        lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, сверить фото заданий и отметить сделанным 👇")
        return "\n".join(lines).strip(), True

    else:
        # --- 18:00 Evening Reminder ---
        active_today = today_tasks if include_today else []
        active_tomorrow = tomorrow_tasks if include_tomorrow else []
        active_2days = in_2_days_tasks if include_2days else []
        active_overdue = overdue_tasks if include_overdue else []
        has_tasks = bool(active_tomorrow or active_2days or active_today or active_overdue)
        if not has_tasks:
            text = (
                f"🌆 <b>Вечерний чек-лист ДЗ</b>\n\n"
                f"📅 <i>{tomorrow_nom}, {tomorrow_label} (завтра)</i>\n\n"
                f"🎉 По выбранным настройкам нет невыполненных заданий!\n"
                f"Все уроки сделаны, приятного вечера и хорошего отдыха ✨"
            )
            return text, False

        lines = [
            f"🌆 <b>Вечерняя проверка домашнего задания {tomorrow_prep}!</b>",
            f"📅 <b>{tomorrow_nom}, {tomorrow_label}</b>",
            ""
        ]
        if active_tomorrow:
            lines.append(f"📚 <b>Задания на завтра ({len(active_tomorrow)}):</b>")
            lines.extend(format_task_list(active_tomorrow))
            lines.append("")

        if active_2days:
            lines.append(f"⏳ <b>Дедлайн через 2 дня ({in_2_days_prep}, {in_2_days_label}) ({len(active_2days)}):</b>")
            lines.extend(format_task_list(active_2days))
            lines.append("")

        if active_today:
            lines.append(f"⏳ <b>Осталось сдать сегодня ({len(active_today)}):</b>")
            lines.extend(format_task_list(active_today))
            lines.append("")

        if active_overdue:
            lines.append(f"⚠️ <b>Не сданные ранее долги ({len(active_overdue)}):</b>")
            lines.extend(format_task_list(active_overdue))
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

async def check_and_send_minutely_reminders():
    """
    Called every minute by background scheduler.
    Dispatches:
    1. Base default reminders at 08:00, 16:00, 18:00 for students with notify_08, notify_16, notify_18 enabled.
    2. Any custom time (e.g. 19:30, 20:00) chosen by students with notify_custom enabled.
    """
    if not bot:
        return
    now_local = datetime.now(TARGET_TZ)
    cur_time_str = now_local.strftime("%H:%M")
    cur_hour = now_local.hour
    cur_min = now_local.minute

    users = get_all_users()
    if not users:
        users = [{"id": 5783100321}]

    sent_users = set()

    for u in users:
        uid = u.get("id")
        if not uid or uid in sent_users:
            continue
        try:
            prefs = get_notification_prefs(uid)
            should_send = False

            # 1. Base default presets at minute 0:
            if cur_min == 0:
                if cur_hour == 8 and prefs.get("notify_08", True):
                    should_send = True
                elif cur_hour == 16 and prefs.get("notify_16", True):
                    should_send = True
                elif cur_hour == 18 and prefs.get("notify_18", True):
                    should_send = True

            # 2. Custom times added by user (e.g. 07:00, 19:30):
            if not should_send and prefs.get("notify_custom", True):
                c_times = prefs.get("custom_times")
                if not c_times and prefs.get("custom_time"):
                    c_times = parse_custom_times(prefs.get("custom_time"))
                if c_times and cur_time_str in c_times:
                    should_send = True

            if should_send:
                text, has_tasks = build_reminder_notification(uid, hour=cur_hour)
                if has_tasks:
                    await bot.send_message(
                        chat_id=uid,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=get_portal_keyboard("🚀 Открыть ДЗ Портал")
                    )
                    sent_users.add(uid)
                    await asyncio.sleep(0.05)
        except Exception as err:
            logger.warning(f"Error checking reminder for user {uid} at {cur_time_str}: {err}")

    # 3. Отправка напоминаний в активные групповые чаты класса
    try:
        active_groups = get_active_group_chats()
        for g in active_groups:
            g_remind_time = normalize_time_str(g.get("remind_time") or "18:00")
            if g_remind_time == cur_time_str:
                await send_group_daily_reminder(g)
    except Exception as g_err:
        logger.warning(f"Error checking group reminders at {cur_time_str}: {g_err}")

def build_weekly_notification(user_id: int, start_date: Optional[date] = None, end_date: Optional[date] = None) -> tuple[str, bool]:
    """
    Builds a weekly summary of all upcoming homework assignments
    grouped by day of the week for the upcoming school week.
    Returns (formatted_html_text, has_tasks).
    """
    now_local = datetime.now(TARGET_TZ)
    today = now_local.date()

    if start_date is None or end_date is None:
        if today.weekday() == 6:  # Воскресенье
            # Предстоящая учебная неделя: пн - вс
            start_date = today + timedelta(days=1)
            end_date = start_date + timedelta(days=6)
        elif today.weekday() == 0:  # Понедельник
            start_date = today
            end_date = today + timedelta(days=6)
        else:
            # Ближайшие 7 дней от сегодня
            start_date = today
            end_date = today + timedelta(days=7)

    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    pending = get_pending_tasks(user_id)
    week_tasks = [t for t in pending if start_str <= str(t.get('due_date', '')) <= end_str]

    start_lbl = f"{start_date.day} {RU_MONTHS[start_date.month]}"
    end_lbl = f"{end_date.day} {RU_MONTHS[end_date.month]}"

    if not week_tasks:
        text = (
            f"🗓️ <b>ДЗ на неделю ({start_lbl} — {end_lbl})</b>\n\n"
            f"🎉 <b>На предстоящую неделю нет невыполненных заданий!</b>\n"
            f"Все уроки готовы или ещё не записаны в расписание. Отличного отдыха! ✨"
        )
        return text, False

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for t in week_tasks:
        d = str(t.get('due_date', ''))
        by_date.setdefault(d, []).append(t)

    sorted_dates = sorted(by_date.keys())
    lines = [
        f"🗓️ <b>ПЛАН Д/З НА ПРЕДСТОЯЩУЮ НЕДЕЛЮ (10-А класс)</b>",
        f"📅 <i>Период: {start_lbl} — {end_lbl}</i>\n"
    ]

    for d_str in sorted_dates:
        d_tasks = by_date[d_str]
        try:
            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            weekday_nom = RU_WEEKDAYS_NOMINATIVE.get(d_obj.weekday(), "День")
            date_lbl = f"{d_obj.day} {RU_MONTHS[d_obj.month]}"
        except Exception:
            weekday_nom = "Срок"
            date_lbl = d_str

        lines.append(f"🔹 <b>{weekday_nom}, {date_lbl} ({len(d_tasks)}):</b>")
        for idx, t in enumerate(d_tasks, 1):
            icon = get_subject_icon(t.get('subject', ''))
            subj = html.escape(str(t.get('subject', 'Предмет')).strip())
            desc = html.escape(str(t.get('description', '')).strip())
            lines.append(f"  {idx}. {icon} <b>{subj}</b>: {desc}")
        lines.append("")

    lines.append(f"📊 Всего заданий на неделю: <b>{len(week_tasks)}</b>")
    lines.append("🚀 Нажмите кнопку ниже, чтобы открыть ДЗ Портал, посмотреть страницы учебников или отметить сделанным 👇")
    return "\n".join(lines).strip(), True

async def send_weekly_reminder():
    """
    Weekly automated digest sent every Sunday at 18:00 (Asia/Tashkent),
    summarizing all homework for the upcoming week.
    """
    if not bot:
        logger.warning("⏰ send_weekly_reminder skipped: Telegram Bot instance is not initialized")
        return

    now_local = datetime.now(TARGET_TZ)
    logger.info(f"📅 Running weekly reminder for upcoming week (local time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')})...")

    users = get_all_users()
    if not users:
        users = [{"id": 5783100321}]

    sent_count = 0
    skipped_count = 0
    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        try:
            prefs = get_notification_prefs(uid)
            if not prefs.get("notify_weekly", True):
                skipped_count += 1
                logger.info(f"⏭ Skipping user {uid}: notify_weekly is disabled")
                continue

            text, has_tasks = build_weekly_notification(uid)
            if has_tasks:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=get_portal_keyboard("🗓️ Открыть Портал на неделю")
                )
                sent_count += 1
                await asyncio.sleep(0.05)
            else:
                skipped_count += 1
        except Exception as e:
            logger.warning(f"Could not send weekly reminder to user {uid}: {e}")

    # 2. Отправка недельного плана в активные групповые чаты класса
    try:
        active_groups = get_active_group_chats()
        for g in active_groups:
            if g.get("is_active", 1):
                await send_group_weekly_reminder(g)
    except Exception as g_err:
        logger.warning(f"Error checking group weekly reminders: {g_err}")

    logger.info(f"📅 Weekly reminder finished: sent {sent_count} notifications, {skipped_count} skipped.")

# ─────────────────────────────────────────────────────────────
# NOTIFICATION SETTINGS HELPERS
# ─────────────────────────────────────────────────────────────

def build_settings_text(prefs) -> str:
    if isinstance(prefs, int):
        prefs = get_notification_prefs(prefs)
    elif not isinstance(prefs, dict):
        prefs = {}
    def st(enabled: bool) -> str:
        return "ВКЛ ✅" if enabled else "ВЫКЛ ❌"

    c_times = prefs.get('custom_times')
    if not c_times and prefs.get('custom_time'):
        c_times = parse_custom_times(prefs.get('custom_time'))
    if not c_times:
        c_times = []

    lines = [
        "⚙️ <b>Настройки личных напоминаний</b>\n",
        "⏰ <b>СТАНДАРТНОЕ ВРЕМЯ (Базовые часы):</b>",
        f"☀️ <b>08:00</b> — Утреннее перед уроками   [{st(prefs.get('notify_08', True))}]",
        f"🎒 <b>16:00</b> — Дневное после уроков       [{st(prefs.get('notify_16', True))}]",
        f"🌆 <b>18:00</b> — Главное вечернее         [{st(prefs.get('notify_18', True))}]",
        f"🗓️ <b>Неделя</b> — Дайджест на вс. 18:00   [{st(prefs.get('notify_weekly', True))}]\n",
        "🕒 <b>СВОЁ ВРЕМЯ (Добавленные часы):</b>"
    ]
    if c_times:
        for ct in c_times:
            lines.append(f"🔔 <b>{ct}</b> — Личное время [ВКЛ ✅]")
    else:
        lines.append("<i>Пока нет добавленных часов. Вы можете добавить любое время (например 07:00 или 19:30).</i>")

    lines.append("\n<i>(Добавить время: <code>/addtime 07:00</code> или кнопкой ниже)</i>")
    if c_times:
        lines.append("<i>(Удалить своё время: нажмите кнопку с ✕ ниже или <code>/deltime 07:00</code>)</i>")
    lines.append("")

    lines.append("📋 <b>КАКИЕ Д/З НАПОМИНАТЬ (Фильтр):</b>")
    lines.append(f"📌 <b>На сегодня</b> — Сдать сегодня        [{st(prefs.get('notify_today', True))}]")
    lines.append(f"📚 <b>На завтра</b> — Сдать завтра         [{st(prefs.get('notify_tomorrow', True))}]")
    lines.append(f"⏳ <b>На послезавтра</b> — За 2 дня до сдачи [{st(prefs.get('notify_2days', True))}]")
    lines.append(f"⚠️ <b>Просроченные</b> — Долги и дедлайны  [{st(prefs.get('notify_overdue', True))}]\n")
    lines.append("<i>Нажми на нужную кнопку ниже, чтобы включить или отключить.</i>")

    return "\n".join(lines)

def build_settings_keyboard(prefs) -> InlineKeyboardMarkup:
    if isinstance(prefs, int):
        prefs = get_notification_prefs(prefs)
    elif not isinstance(prefs, dict):
        prefs = {}
    def label(key: str, emoji: str, time: str) -> str:
        on = prefs.get(key, True)
        return f"{emoji} {time} {'✅' if on else '❌'}"

    c_times = prefs.get('custom_times')
    if not c_times and prefs.get('custom_time'):
        c_times = parse_custom_times(prefs.get('custom_time'))
    if not c_times:
        c_times = []

    rows = [
        [
            InlineKeyboardButton(text=label("notify_08", "☀️", "08:00"), callback_data="toggle_notif:08"),
            InlineKeyboardButton(text=label("notify_16", "🎒", "16:00"), callback_data="toggle_notif:16"),
            InlineKeyboardButton(text=label("notify_18", "🌆", "18:00"), callback_data="toggle_notif:18")
        ]
    ]

    # Ряд для удаляемых своих времён (по 2 в ряд)
    if c_times:
        time_buttons = [InlineKeyboardButton(text=f"🔔 {ct} ✕", callback_data=f"del_time:{ct}") for ct in c_times]
        for i in range(0, len(time_buttons), 2):
            rows.append(time_buttons[i:i+2])

    rows.append([
        InlineKeyboardButton(text="➕ Добавить своё время", callback_data="add_time_menu"),
        InlineKeyboardButton(text=label("notify_weekly", "🗓️", "Дайджест (вс)"), callback_data="toggle_notif:weekly")
    ])

    rows.append([
        InlineKeyboardButton(text=label("notify_today", "📌", "На сегодня"), callback_data="toggle_notif:today"),
        InlineKeyboardButton(text=label("notify_tomorrow", "📚", "На завтра"), callback_data="toggle_notif:tomorrow")
    ])

    rows.append([
        InlineKeyboardButton(text=label("notify_2days", "⏳", "На послезавтра"), callback_data="toggle_notif:2days"),
        InlineKeyboardButton(text=label("notify_overdue", "⚠️", "Просроченные"), callback_data="toggle_notif:overdue")
    ])

    rows.append([
        InlineKeyboardButton(text="⚙️ Открыть Настройки в WebApp", web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL))
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(Command("settings", "уведомления", "notif"))
async def handle_settings(message: Message):
    record_user_activity(message.from_user)
    prefs = get_notification_prefs(message.from_user.id)
    await safe_reply(
        message,
        build_settings_text(prefs),
        reply_markup=build_settings_keyboard(prefs)
    )

@dp.message(Command("addtime", "добавить"))
async def handle_add_time(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    arg = command.args.strip() if command and command.args else ""
    norm = normalize_time_str(arg)
    if not norm:
        await message.answer(
            "❌ Укажите корректное время в формате <b>ЧЧ:ММ</b> или час, например:\n"
            "• <code>/addtime 07:00</code>\n"
            "• <code>/addtime 7</code>\n"
            "• <code>/addtime 19:30</code>"
        )
        return
    uid = message.from_user.id
    prefs = get_notification_prefs(uid)
    c_times = list(prefs.get("custom_times", []))
    if norm not in c_times:
        c_times.append(norm)
    c_times.sort()
    upsert_notification_prefs(
        user_id=uid,
        notify_08=prefs.get("notify_08", True),
        notify_16=prefs.get("notify_16", True),
        notify_18=prefs.get("notify_18", True),
        notify_2days=prefs.get("notify_2days", True),
        notify_weekly=prefs.get("notify_weekly", True),
        notify_today=prefs.get("notify_today", True),
        notify_tomorrow=prefs.get("notify_tomorrow", True),
        notify_overdue=prefs.get("notify_overdue", True),
        notify_custom=True,
        custom_times=c_times
    )
    updated = get_notification_prefs(uid)
    await message.answer(
        f"✅ Время <b>{norm}</b> успешно добавлено в ваше расписание напоминаний!\n\n"
        f"Бот будет напоминать о заданиях в это время.",
        reply_markup=build_settings_keyboard(updated)
    )

@dp.message(Command("deltime", "удалить"))
async def handle_del_time(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    arg = command.args.strip() if command and command.args else ""
    norm = normalize_time_str(arg)
    if not norm:
        await message.answer(
            "❌ Укажите время для удаления (например, <code>/deltime 07:00</code>)."
        )
        return
    uid = message.from_user.id
    prefs = get_notification_prefs(uid)
    c_times = [t for t in prefs.get("custom_times", []) if t != norm]
    upsert_notification_prefs(
        user_id=uid,
        notify_08=prefs.get("notify_08", True),
        notify_16=prefs.get("notify_16", True),
        notify_18=prefs.get("notify_18", True),
        notify_2days=prefs.get("notify_2days", True),
        notify_weekly=prefs.get("notify_weekly", True),
        notify_today=prefs.get("notify_today", True),
        notify_tomorrow=prefs.get("notify_tomorrow", True),
        notify_overdue=prefs.get("notify_overdue", True),
        notify_custom=bool(c_times),
        custom_times=c_times
    )
    updated = get_notification_prefs(uid)
    await message.answer(
        f"🗑️ Время <b>{norm}</b> удалено из вашего расписания.",
        reply_markup=build_settings_keyboard(updated)
    )

@dp.message(Command("time", "время", "times"))
async def handle_set_time(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    arg = command.args.strip() if command and command.args else ""
    if not arg:
        uid = message.from_user.id
        prefs = get_notification_prefs(uid)
        await message.answer(
            build_settings_text(prefs),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(prefs)
        )
        return
    await handle_add_time(message, command)

def format_leaderboard_text(leaders: List[Dict[str, Any]]) -> str:
    if not leaders:
        return (
            "🏆 <b>Таблица лидеров 10-А класса</b>\n\n"
            "Пока нет данных об активности. Добавляйте задания и загружайте фото, чтобы занять 1-е место!"
        )
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [
        "🏆 <b>Таблица лидеров редакторов 10-А класса</b>\n",
        "<i>Баллы начисляются за вклад в общее дело:</i>",
        "➕ Задание: <b>+5</b> | 📸 Фото/конспект: <b>+3</b> | ✅ Выполнение: <b>+1</b>\n"
    ]
    for item in leaders[:10]:
        rank = item["rank"]
        medal = medals.get(rank, f"<b>{rank}.</b>")
        name = html.escape(str(item.get("name", "Ученик")))
        badge = item.get("badge", "")
        score = item["score"]
        t = item["tasks"]
        m = item["materials"]
        c = item["completions"]
        lines.append(f"{medal} <b>{name}</b> — <b>{score}</b> очк. ({badge})")
        lines.append(f"    └ 📝 Заданий: {t} | 📸 Фото: {m} | ✅ Сдано: {c}")
    return "\n".join(lines)

@dp.callback_query()
async def handle_done_callback(callback: CallbackQuery):
    # ── Delete a custom notification time ───────────────────
    if callback.data.startswith("del_time:"):
        time_to_del = callback.data.split(":", 1)[1]
        uid = callback.from_user.id
        prefs = get_notification_prefs(uid)
        c_times = [t for t in prefs.get("custom_times", []) if t != time_to_del]
        upsert_notification_prefs(
            user_id=uid,
            notify_08=prefs.get("notify_08", True),
            notify_16=prefs.get("notify_16", True),
            notify_18=prefs.get("notify_18", True),
            notify_2days=prefs.get("notify_2days", True),
            notify_weekly=prefs.get("notify_weekly", True),
            notify_today=prefs.get("notify_today", True),
            notify_tomorrow=prefs.get("notify_tomorrow", True),
            notify_overdue=prefs.get("notify_overdue", True),
            notify_custom=bool(c_times),
            custom_times=c_times
        )
        await callback.answer(f"🗑️ Время {time_to_del} удалено!")
        updated = get_notification_prefs(uid)
        await callback.message.edit_text(
            build_settings_text(updated),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(updated)
        )
        return

    # ── Interactive Add Time Menu ───────────────────────────
    if callback.data == "add_time_menu":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="+ 07:00", callback_data="add_quick_time:07:00"),
                InlineKeyboardButton(text="+ 15:00", callback_data="add_quick_time:15:00"),
                InlineKeyboardButton(text="+ 19:00", callback_data="add_quick_time:19:00"),
            ],
            [
                InlineKeyboardButton(text="+ 20:00", callback_data="add_quick_time:20:00"),
                InlineKeyboardButton(text="+ 21:00", callback_data="add_quick_time:21:00"),
                InlineKeyboardButton(text="+ 22:00", callback_data="add_quick_time:22:00"),
            ],
            [
                InlineKeyboardButton(text="🔙 Назад в настройки", callback_data="back_to_settings")
            ]
        ])
        await callback.answer()
        await callback.message.edit_text(
            "🕒 <b>Добавление своего времени напоминания</b>\n\n"
            "Нажмите на нужное время ниже, либо отправьте команду в чат:\n"
            "• <code>/addtime 07:00</code>\n"
            "• <code>/addtime 19:30</code>\n"
            "• <code>/addtime 7</code>\n\n"
            "Также вы можете добавлять любое время в ДЗ Портале в окне <b>⚙️ Настройки</b>.",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    if callback.data.startswith("add_quick_time:"):
        t_add = callback.data.split(":", 1)[1]
        uid = callback.from_user.id
        prefs = get_notification_prefs(uid)
        c_times = list(prefs.get("custom_times", []))
        if t_add not in c_times:
            c_times.append(t_add)
        c_times.sort()
        upsert_notification_prefs(
            user_id=uid,
            notify_08=prefs.get("notify_08", True),
            notify_16=prefs.get("notify_16", True),
            notify_18=prefs.get("notify_18", True),
            notify_2days=prefs.get("notify_2days", True),
            notify_weekly=prefs.get("notify_weekly", True),
            notify_today=prefs.get("notify_today", True),
            notify_tomorrow=prefs.get("notify_tomorrow", True),
            notify_overdue=prefs.get("notify_overdue", True),
            notify_custom=True,
            custom_times=c_times
        )
        await callback.answer(f"✅ Добавлено {t_add}!")
        updated = get_notification_prefs(uid)
        await callback.message.edit_text(
            build_settings_text(updated),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(updated)
        )
        return

    if callback.data == "back_to_settings":
        uid = callback.from_user.id
        prefs = get_notification_prefs(uid)
        await callback.answer()
        await callback.message.edit_text(
            build_settings_text(prefs),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(prefs)
        )
        return

    # ── Toggle notification preference ──────────────────────
    if callback.data.startswith("toggle_notif:"):
        hour = callback.data.split(":")[1]  # "08", "16", "18", "2days", "weekly", "today", "tomorrow", "overdue"
        uid = callback.from_user.id
        prefs = get_notification_prefs(uid)
        key = f"notify_{hour}"
        prefs[key] = not prefs.get(key, True)
        upsert_notification_prefs(
            user_id=uid,
            notify_08=prefs.get("notify_08", True),
            notify_16=prefs.get("notify_16", True),
            notify_18=prefs.get("notify_18", True),
            notify_2days=prefs.get("notify_2days", True),
            notify_weekly=prefs.get("notify_weekly", True),
            notify_today=prefs.get("notify_today", True),
            notify_tomorrow=prefs.get("notify_tomorrow", True),
            notify_overdue=prefs.get("notify_overdue", True),
            notify_custom=prefs.get("notify_custom", True),
            custom_time=prefs.get("custom_time", ""),
            custom_times=prefs.get("custom_times", [])
        )
        await callback.answer("✅ Сохранено!")
        updated = get_notification_prefs(uid)
        await callback.message.edit_text(
            build_settings_text(updated),
            parse_mode="HTML",
            reply_markup=build_settings_keyboard(updated)
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

    if callback.data == "open_leaderboard":
        await callback.answer()
        leaders = get_leaderboard("10-А")
        text = format_leaderboard_text(leaders)
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=get_portal_keyboard("🏆 Открыть рейтинг в приложении")
        )
        return

    if callback.data.startswith("done:"):
        task_id = int(callback.data.split(":")[1])
        mark_task_done(task_id, callback.from_user.id)
        await callback.answer("Задание выполнено! 🎉")
        await callback.message.edit_text(f"Задание #{task_id} выполнено! ✅")

@dp.message(CommandStart())
async def handle_start(message: Message):
    # В групповых чатах команда /start отключена и игнорируется
    if message.chat.type in ("group", "supergroup", "channel"):
        return

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
        [InlineKeyboardButton(text="🚀 Открыть ДЗ Портал", web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL))],
        [
            InlineKeyboardButton(text="🏆 Таблица лидеров", callback_data="open_leaderboard"),
            InlineKeyboardButton(text="⚙️ Уведомления", callback_data="open_settings")
        ]
    ])
    await safe_reply(message, text, reply_markup=start_keyboard)

@dp.message(Command("top", "leaderboard", "rating", "рейтинг"))
async def handle_leaderboard_command(message: Message):
    record_user_activity(message.from_user)
    leaders = get_leaderboard("10-А")
    text = format_leaderboard_text(leaders)
    await safe_reply(
        message,
        text,
        reply_markup=get_portal_keyboard("🏆 Открыть рейтинг в приложении", chat_or_type=message)
    )

@dp.message(Command("remind", "notify"))
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
    await safe_reply(message, text, reply_markup=get_portal_keyboard(btn_text, chat_or_type=message))

# ─────────────────────────────────────────────────────────────
# ОБРАБОТКА ДОБАВЛЕНИЯ БОТА В ГРУППОВЫЕ ЧАТЫ КЛАССА
# ─────────────────────────────────────────────────────────────

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_joined_group(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type in ("group", "supergroup"):
        user = event.from_user
        upsert_group_chat(
            chat_id=chat.id,
            title=chat.title or "Групповой чат",
            chat_type=chat.type,
            added_by=user.id if user else None,
            is_active=1,
            remind_time="18:00",
            class_name="10-А"
        )
        welcome_text = (
            f"👋 <b>Всем привет! Я помощник по домашним заданиям для {html.escape(chat.title or 'вашего класса')}!</b>\n\n"
            "Я буду помогать классу не забывать про уроки и быстро находить Д/З прямо здесь.\n\n"
            "📌 <b>Что я умею в группе:</b>\n"
            "• <b>/tomorrow</b> (или <b>/завтра</b>, <b>/dz</b>) — показать всё Д/З на завтра\n"
            "• <b>/tomorrow [предмет]</b> (например: <code>/dz алгебра</code>) — показать задание и фото страниц\n"
            "• <b>/today</b> (или <b>/сегодня</b>) — показать Д/З на сегодня\n"
            "• <b>/remind_group</b> — настройки вечернего напоминания в группу (по умолчанию в 18:00)\n\n"
            "💡 <i>Также можно просто спросить в чате: «Что завтра по алгебре?» или «Какое дз на завтра?».</i>"
        )
        btn = get_portal_keyboard("🎒 Открыть ДЗ Портал", chat_or_type="group")
        try:
            await event.bot.send_message(chat.id, welcome_text, parse_mode="HTML", reply_markup=btn)
            logger.info(f"✅ Bot joined group {chat.id} ({chat.title}) and sent welcome message.")
        except Exception as e:
            logger.warning(f"Could not send welcome message to group {chat.id} with markup: {e}")
            try:
                await event.bot.send_message(chat.id, welcome_text, parse_mode="HTML")
            except Exception:
                pass

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_bot_left_group(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup"):
        set_group_reminder(event.chat.id, is_active=False)
        logger.info(f"Bot was removed from group {event.chat.id} ({event.chat.title})")

# ─────────────────────────────────────────────────────────────
# КОМАНДЫ Д/З: /tomorrow, /today, /remind_group
# ─────────────────────────────────────────────────────────────

@dp.message(Command("tomorrow", "завтра", "dz", "дз", "hw", "tasks"))
async def handle_tomorrow(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    now_local = datetime.now(TARGET_TZ)
    tomorrow = now_local.date() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
    tomorrow_nom = RU_WEEKDAYS_NOMINATIVE.get(tomorrow.weekday(), "Завтра")
    tomorrow_label = f"{tomorrow.day} {RU_MONTHS[tomorrow.month]}"

    if command and command.args:
        arg_subject = command.args.strip()
        await send_subject_homework(message, subject_query=arg_subject, target_date_str=tomorrow_str)
        return

    tasks = get_class_tasks_for_date(tomorrow_str, class_name="10-А")

    if not tasks:
        upcoming_lines = []
        try:
            pending = get_pending_tasks(message.from_user.id if message.chat.type == "private" else 0, class_name="10-А")
            upcoming_list = [t for t in pending if str(t.get("due_date", "")) > tomorrow_str][:3]
            for t in upcoming_list:
                s_name = t.get("subject", "Предмет")
                d_date = t.get("due_date", "")
                t_desc = str(t.get("description", "")).strip()
                t_short = f" — {t_desc[:35]}..." if len(t_desc) > 35 else (f" — {t_desc}" if t_desc else "")
                upcoming_lines.append(f"• <b>{html.escape(s_name)}</b> ({d_date}){html.escape(t_short)}")
        except Exception:
            pass

        upcoming_block = ""
        if upcoming_lines:
            upcoming_block = "\n\n📌 <b>Ближайшие задания:</b>\n" + "\n".join(upcoming_lines)

        text = (
            f"🎉 <b>На завтра ({tomorrow_nom}, {tomorrow_label}) ничего не задано!</b>\n\n"
            f"Все уроки на завтра сделаны или ещё не записаны. Отличный повод отдохнуть!{upcoming_block}\n\n"
            f"Если учитель задал домашку — добавьте её через кнопку ниже."
        )
        btn = get_portal_keyboard("🎒 Открыть ДЗ Портал", chat_or_type=message)
        await safe_reply(message, text, reply_markup=btn)
        return

    count_subj = len(tasks)
    header_text = (
        f"📚 <b>Домашнее задание на завтра!</b>\n"
        f"📅 <b>{tomorrow_nom}, {tomorrow_label}</b> ({count_subj} {format_plural_ru(count_subj, 'предмет', 'предмета', 'предметов')}):\n"
        f"<i>Отправляю каждое задание отдельно с фотографиями страниц 👇</i>"
    )
    await safe_reply(message, header_text)

    for idx, t in enumerate(tasks, start=1):
        await send_single_task_card(
            message=message,
            task=t,
            date_badge=f"📅 <b>{tomorrow_nom}, {tomorrow_label}</b>",
            index=idx,
            total=count_subj
        )
        await asyncio.sleep(0.3)

@dp.message(Command("remind_group", "group_remind"))
async def handle_remind_group(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        await message.answer(
            "ℹ️ Команда <b>/remind_group</b> предназначена для групповых чатов класса.\n"
            "Добавьте бота в чат класса и настройте автоматические напоминания!",
            parse_mode="HTML"
        )
        return

    group_info = get_group_chat(chat.id)
    if not group_info:
        upsert_group_chat(
            chat_id=chat.id,
            title=chat.title or "Группа класса",
            chat_type=chat.type,
            added_by=message.from_user.id if message.from_user else None,
            is_active=1,
            remind_time="18:00"
        )
        group_info = get_group_chat(chat.id) or {}

    cur_active = bool(group_info.get("is_active", 1))
    cur_time = group_info.get("remind_time", "18:00")

    if command and command.args:
        arg = command.args.strip().lower()
        if arg in ("off", "0", "выкл", "disable", "stop"):
            set_group_reminder(chat.id, is_active=False)
            await message.answer(
                "🔕 <b>Ежедневные напоминания в этой группе выключены.</b>\n"
                "Чтобы включить обратно, отправьте: <code>/remind_group on</code>",
                parse_mode="HTML"
            )
            return
        elif arg in ("on", "1", "вкл", "enable", "start"):
            set_group_reminder(chat.id, is_active=True)
            await message.answer(
                f"🔔 <b>Ежедневные напоминания в группе включены!</b>\n"
                f"⏰ Время отправки: <b>{cur_time}</b> (Asia/Tashkent).\n"
                f"Бот будет каждый вечер присылать список Д/З на завтра.",
                parse_mode="HTML"
            )
            return
        else:
            norm = normalize_time_str(arg)
            if norm:
                set_group_reminder(chat.id, is_active=True, remind_time=norm)
                await message.answer(
                    f"⏰ <b>Время вечернего напоминания в группе изменено на {norm}!</b>\n"
                    f"Каждый день в {norm} бот будет публиковать уроки на завтра.",
                    parse_mode="HTML"
                )
                return
            else:
                await message.answer(
                    "❌ Неверный формат времени. Используйте ЧЧ:ММ (например: <code>/remind_group 18:30</code>) или <code>/remind_group on/off</code>.",
                    parse_mode="HTML"
                )
                return

    status_str = "🔔 <b>Включены</b>" if cur_active else "🔕 <b>Выключены</b>"
    text = (
        f"⚙️ <b>Настройки напоминаний для группы «{html.escape(chat.title or '')}»:</b>\n\n"
        f"• Статус: {status_str}\n"
        f"• Время отправки: <b>{cur_time}</b> (Asia/Tashkent)\n\n"
        f"📌 <b>Управление:</b>\n"
        f"• <code>/remind_group off</code> — выключить напоминания\n"
        f"• <code>/remind_group on</code> — включить напоминания\n"
        f"• <code>/remind_group 19:00</code> — изменить время напоминания\n"
    )
    await message.answer(text, parse_mode="HTML")

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

@dp.message(Command("today", "сегодня"))
async def handle_today_tasks(message: Message, command: CommandObject = None):
    record_user_activity(message.from_user)
    today = get_local_today()
    today_str = today.isoformat()
    weekday_nom = RU_WEEKDAYS_NOMINATIVE.get(today.weekday(), "Сегодня")
    date_label = f"{today.day} {RU_MONTHS[today.month]}"

    if command and command.args:
        arg_subject = command.args.strip()
        await send_subject_homework(message, subject_query=arg_subject, target_date_str=today_str)
        return

    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        tasks = get_class_tasks_for_date(today_str, class_name="10-А")
    else:
        tasks = get_today_tasks(message.from_user.id, today_str)

    if not tasks:
        await safe_reply(
            message,
            f"🎉 <b>На сегодня ({weekday_nom}, {date_label}) нет заданий!</b>\n\n"
            f"На сегодня ничего не осталось. Ты свободен! Отдыхай или загляни в ДЗ Портал 👇",
            reply_markup=get_portal_keyboard(chat_or_type=message)
        )
    else:
        count_subj = len(tasks)
        header_text = (
            f"📋 <b>Задания на сегодня ({weekday_nom}, {date_label}) [{count_subj}]:</b>\n"
            f"<i>Отправляю каждое задание отдельно с фотографиями страниц 👇</i>"
        )
        await safe_reply(message, header_text)

        for idx, t in enumerate(tasks, start=1):
            await send_single_task_card(
                message=message,
                task=t,
                date_badge=f"📅 <b>Сегодня ({weekday_nom}, {date_label})</b>",
                index=idx,
                total=count_subj
            )
            await asyncio.sleep(0.3)

@dp.message(Command("overdue"))
async def handle_overdue(message: Message):
    record_user_activity(message.from_user)
    today_str = get_local_today().isoformat()
    overdue = get_overdue_tasks(message.from_user.id, today_str)

    if not overdue:
        await safe_reply(message, "✨ <b>Просроченных заданий нет! Вы молодец!</b>", reply_markup=get_portal_keyboard(chat_or_type=message))
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
        await safe_reply(message, "\n".join(lines), reply_markup=get_portal_keyboard(chat_or_type=message))

@dp.message(Command("priority", "pending"))
async def handle_priority(message: Message):
    record_user_activity(message.from_user)
    today = get_local_today()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()
    in2days_str = (today + timedelta(days=2)).isoformat()
    tasks = get_pending_tasks(message.from_user.id)

    if not tasks:
        await safe_reply(message, "🎉 <b>У вас нет невыполненных заданий! Всё чисто.</b>", reply_markup=get_portal_keyboard(chat_or_type=message))
        return

    p_today, p_tomorrow, p_in2days, p_other = [], [], [], []
    for t in tasks:
        d = str(t.get('due_date', ''))
        if d <= today_str:
            p_today.append(t)
        elif d == tomorrow_str:
            p_tomorrow.append(t)
        elif d == in2days_str:
            p_in2days.append(t)
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

    if p_in2days:
        lines.append(f"⏳ <b>Через 2 дня ({len(p_in2days)}):</b>")
        for t in p_in2days:
            lines.append(f"• {get_subject_icon(t.get('subject', ''))} <b>{html.escape(str(t.get('subject', '')))}</b>: {html.escape(str(t.get('description', '')))}")
        lines.append("")

    if p_other:
        lines.append(f"📅 <b>На другие дни ({len(p_other)}):</b>")
        for t in p_other:
            lines.append(f"• {get_subject_icon(t.get('subject', ''))} <b>{html.escape(str(t.get('subject', '')))}</b> ({t.get('due_date')}): {html.escape(str(t.get('description', '')))}")
        lines.append("")

    lines.append("🚀 Открыть приложение:")
    await safe_reply(message, "\n".join(lines), reply_markup=get_portal_keyboard(chat_or_type=message))

@dp.message(Command("week", "weekly", "неделя"))
async def handle_weekly_command(message: Message):
    record_user_activity(message.from_user)
    text, has_tasks = build_weekly_notification(message.from_user.id)
    await safe_reply(
        message,
        text,
        reply_markup=get_portal_keyboard("🗓️ Открыть Портал на неделю", chat_or_type=message)
    )

@dp.message(Command("broadcast_weekly", "notify_week"))
async def handle_broadcast_weekly(message: Message):
    record_user_activity(message.from_user)
    await safe_reply(message, "🔄 Запуск рассылки недельного дайджеста...")
    await send_weekly_reminder()
    await safe_reply(message, "✅ Рассылка недельного дайджеста завершена!")

@dp.message(Command("add"))
async def handle_add(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args:
        await safe_reply(message, "Использование: /add <предмет> <ГГГГ-ММ-ДД> <описание>\nПример: /add Алгебра 2026-11-16 Стр. 45 № 1-5")
        return

    args = command.args.split(maxsplit=2)
    if len(args) < 3:
        await safe_reply(message, "Использование: /add <предмет> <ГГГГ-ММ-ДД> <описание>")
        return

    subject, due_date, description = args[0], args[1], args[2]
    user = message.from_user
    nick = f"@{user.username}" if user.username else (user.first_name or "Ученик")
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    task_id = add_task(user.id, subject, description, due_date, telegram_nick=nick, full_name=full_name)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отметить сделанным", callback_data=f"done:{task_id}")],
        [InlineKeyboardButton(text="🚀 Открыть ДЗ Портал", web_app=WebAppInfo(url=WEB_APP_LAUNCH_URL))]
    ])

    await safe_reply(
        message,
        f"✅ Добавлено #{task_id}: [{subject}] {description} (Дедлайн: {due_date})",
        reply_markup=keyboard
    )

@dp.message(Command("done"))
async def handle_done(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await safe_reply(message, "Использование: /done <номер_задания>\nПример: /done 2")
        return

    task_id = int(command.args)
    mark_task_done(task_id, message.from_user.id)
    await safe_reply(message, f"🎉 Отлично! Задание #{task_id} отмечено выполненным.", reply_markup=get_portal_keyboard(chat_or_type=message))

@dp.message(Command("delete"))
async def handle_delete(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await safe_reply(message, "Использование: /delete <номер_задания>\nПример: /delete 2")
        return

    task_id = int(command.args)
    success, msg = delete_task(task_id, message.from_user.id)
    if not success:
        await safe_reply(message, f"❌ {msg}")
        return
    await safe_reply(message, f"🗑️ Задание #{task_id} удалено.")

@dp.message(Command("delete_all"))
async def handle_delete_all(message: Message):
    record_user_activity(message.from_user)
    delete_all_tasks(message.from_user.id)
    await safe_reply(message, "🗑️ Все ваши задания успешно удалены.")

@dp.message(Command("materials"))
async def handle_materials(message: Message, command: CommandObject):
    record_user_activity(message.from_user)
    if not command.args or not command.args.isdigit():
        await safe_reply(message, "Использование: /materials <номер_задания>")
        return

    task_id = int(command.args)
    materials = get_materials_for_task(task_id)

    if not materials:
        await safe_reply(message, f"К заданию #{task_id} пока не прикреплено фотоматериалов.", reply_markup=get_portal_keyboard(chat_or_type=message))
    else:
        text = f"📎 <b>Материалы к заданию #{task_id} ({len(materials)} шт.):</b>\n"
        for m in materials:
            text += f"- [{m['file_id']}] {m.get('caption', '')} (Автор: {m.get('uploader_nick', '')})\n"
        await safe_reply(message, text, reply_markup=get_portal_keyboard(chat_or_type=message))

@dp.message(Command("help"))
async def handle_help(message: Message):
    record_user_activity(message.from_user)
    help_text = (
        "🤖 <b>Команды ДЗ Бота 10-го класса:</b>\n\n"
        "• <b>/start</b> — Главное меню и запуск приложения\n"
        "• <b>/top</b> — 🏆 Таблица лидеров и самых активных редакторов\n"
        "• <b>/tomorrow</b> (или <b>/завтра</b>) — Задания на завтра\n"
        "• <b>/tomorrow [предмет]</b> — Д/З по конкретному предмету (с фото!)\n"
        "• <b>/today</b> (или <b>/сегодня</b>) — Задания на сегодня\n"
        "• <b>/priority</b> — Все невыполненные задания по дням\n"
        "• <b>/settings</b> — Настройки личных уведомлений\n"
        "• <b>/remind_group</b> — Напоминание в групповой чат прямо сейчас\n"
        "• <b>/overdue</b> — Просроченные задания\n"
        "• <b>/done &lt;id&gt;</b> — Отметить задание выполненным\n\n"
        "💡 <i>В группе можно просто спросить: «что задали на завтра по алгебре?» или «что на завтра?» — бот всё поймет и покажет!</i>"
    )
    await safe_reply(message, help_text, reply_markup=get_portal_keyboard(chat_or_type=message))

@dp.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    """Ручная очистка заданий старше 30 дней."""
    record_user_activity(message.from_user)
    deleted = cleanup_old_tasks(days=30)
    await message.answer(
        f"🧹 <b>Очистка устаревших заданий завершена!</b>\n\n"
        f"Удалено заданий старше 1 месяца (> 30 дней): <b>{deleted}</b>\n"
        f"База данных и кэш оптимизированы.",
        parse_mode="HTML"
    )

# --- Умная обработка текстовых запросов и fallback ---
@dp.message()
async def handle_text_fallback(message: Message):
    record_user_activity(message.from_user)
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text:
        return

    # Запасная обработка команд со слэшем (на случай если Command filter не сработал из-за формата mention)
    if raw_text.startswith("/"):
        parts = raw_text.split(maxsplit=1)
        cmd_part = parts[0].lower().lstrip("/")
        cmd_clean = cmd_part.split("@")[0]
        cmd_args = parts[1].strip() if len(parts) > 1 else None

        if cmd_clean in ("tomorrow", "завтра", "dz", "дз", "hw", "tasks"):
            fake_cmd = CommandObject(prefix="/", command=cmd_clean, args=cmd_args)
            await handle_tomorrow(message, command=fake_cmd)
            return
        elif cmd_clean in ("today", "сегодня"):
            fake_cmd = CommandObject(prefix="/", command=cmd_clean, args=cmd_args)
            await handle_today_tasks(message, command=fake_cmd)
            return
        elif cmd_clean in ("remind_group", "group_remind"):
            fake_cmd = CommandObject(prefix="/", command=cmd_clean, args=cmd_args)
            await handle_remind_group(message, command=fake_cmd)
            return

    is_group = message.chat.type in ("group", "supergroup")
    bot_info = None
    try:
        bot_info = await bot.get_me() if bot else None
    except Exception:
        pass
    bot_username = (bot_info.username.lower() if bot_info and bot_info.username else "")

    # Проверка упоминания бота или ответа боту в группе
    is_mentioned = False
    if is_group:
        if bot_username and f"@{bot_username}" in raw_text.lower():
            is_mentioned = True
        elif message.reply_to_message and bot_info and message.reply_to_message.from_user.id == bot_info.id:
            is_mentioned = True

    # Регулярные выражения для распознавания вопросов о Д/З
    HW_QUERY_PATTERNS = [
        r'(?:что|какое|какие|есть ли|скиньте|подскажите)\s+(?:задали|задано|дз|д\/з|домашк[аеу]|задани[ея]|уроки?)',
        r'^(?:дз|д\/з|домашка)\s+(?:по\s+)?(\w+)',
        r'(?:что|какое дз|какие уроки)\s+(?:на\s+)?(?:завтра|сегодня)',
        r'(?:что|как)\s+там\s+(?:по\s+)?(\w+)',
        r'по\s+([а-яa-z\-]+)\s+(?:что|какое|задали)',
        r'что\s+(?:завтра|сегодня)\s+(?:по\s+)?(\w+)',
        r'что\s+по\s+([а-яa-z\-]+)',
    ]
    is_hw_query = any(re.search(pat, raw_text.lower()) for pat in HW_QUERY_PATTERNS)

    if is_group and not is_mentioned and not is_hw_query:
        # В групповом чате бот не спамит и игнорирует обычные разговоры одноклассников
        return

    now_local = datetime.now(TARGET_TZ)
    today = now_local.date()
    today_str = today.isoformat()
    tomorrow = today + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()

    # Определение целевой даты (по умолчанию "завтра", если явно не сказано "сегодня")
    target_date = today_str if ("сегодня" in raw_text.lower()) else tomorrow_str

    # Поиск предмета в тексте (например: "алгебра", "физика", "геометрия")
    found_subject = find_subject_in_text(raw_text)

    if found_subject:
        await send_subject_homework(message, subject_query=found_subject, target_date_str=target_date)
        return

    # Если спросили в общем про завтра / сегодня или упомянули бота
    if "сегодня" in raw_text.lower() or "завтра" in raw_text.lower() or is_hw_query or is_mentioned:
        if "сегодня" in raw_text.lower():
            tasks = get_class_tasks_for_date(today_str, class_name="10-А")
            day_title = "сегодня"
        else:
            tasks = get_class_tasks_for_date(tomorrow_str, class_name="10-А")
            day_title = "завтра"

        if not tasks:
            upcoming_lines = []
            try:
                pending = get_pending_tasks(message.from_user.id if not is_group else 0, class_name="10-А")
                target_str = today_str if day_title == "сегодня" else tomorrow_str
                upcoming_list = [t for t in pending if str(t.get("due_date", "")) > target_str][:3]
                for t in upcoming_list:
                    s_name = t.get("subject", "Предмет")
                    d_date = t.get("due_date", "")
                    t_desc = str(t.get("description", "")).strip()
                    t_short = f" — {t_desc[:35]}..." if len(t_desc) > 35 else (f" — {t_desc}" if t_desc else "")
                    upcoming_lines.append(f"• <b>{html.escape(s_name)}</b> ({d_date}){html.escape(t_short)}")
            except Exception:
                pass

            upcoming_block = ""
            if upcoming_lines:
                upcoming_block = "\n\n📌 <b>Ближайшие задания:</b>\n" + "\n".join(upcoming_lines)

            btn = get_portal_keyboard("🎒 Открыть ДЗ Портал", chat_or_type=message)
            await safe_reply(
                message,
                f"🎉 <b>На {day_title} уроков не задано!</b>{upcoming_block}\n\nОтдыхайте или загляните в ДЗ Портал 👇",
                reply_markup=btn
            )
            return

        header_text = (
            f"📚 <b>Домашнее задание на {day_title}!</b> ({len(tasks)} {format_plural_ru(len(tasks), 'предмет', 'предмета', 'предметов')}):\n"
            f"<i>Отправляю каждое задание отдельно с фотографиями страниц 👇</i>"
        )
        await safe_reply(message, header_text)

        date_badge_text = f"📅 <b>{day_title.capitalize()}</b>"
        for idx, t in enumerate(tasks, start=1):
            await send_single_task_card(
                message=message,
                task=t,
                date_badge=date_badge_text,
                index=idx,
                total=len(tasks)
            )
            await asyncio.sleep(0.3)
        return

    # Для личного чата: общее приветствие и кнопка WebApp
    if not is_group:
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
        btn = get_portal_keyboard("🚀 Открыть ДЗ Портал", chat_or_type=message)
        await safe_reply(message, text, reply_markup=btn)

# --- Глобальный обработчик ошибок ---
@dp.error()
async def global_error_handler(event):
    logger.exception(f"⚠️ Unhandled bot error: {getattr(event, 'exception', event)}")
    try:
        update = getattr(event, "update", None)
        msg = getattr(update, "message", None) if update else None
        if msg:
            await safe_reply(
                msg,
                "⚠️ <b>Произошла временная ошибка при обработке команды.</b>\n"
                "Пожалуйста, попробуйте ещё раз через пару секунд."
            )
    except Exception:
        pass

async def main():
    if not bot:
        logger.error("❌ BOT_TOKEN is missing! Please configure BOT_TOKEN in .env or Render Environment Variables.")
        return
    init_db()
    # Sync global menu button, short description and commands on startup
    try:
        await sync_bot_profile()
    except Exception as e:
        logger.warning(f"Could not sync bot profile on startup: {e}")

    # 1. Start background reminder scheduler (checks every minute in Asia/Tashkent)
    scheduler = AsyncIOScheduler(timezone=TARGET_TZ)
    # Поминутная проверка базовых (08:00, 16:00, 18:00) и пользовательских времён напоминаний
    scheduler.add_job(check_and_send_minutely_reminders, 'cron', minute='*', misfire_grace_time=60)
    # Недельный дайджест со всеми ДЗ на предстоящую неделю каждое воскресенье в 18:00
    scheduler.add_job(send_weekly_reminder, 'cron', day_of_week='sun', hour=18, minute=0)
    # Ежедневная автоочистка заданий старше 30 дней в 03:30 ночи
    scheduler.add_job(lambda: cleanup_old_tasks(days=30), 'cron', hour=3, minute=30)
    scheduler.start()
    logger.info(f"⏰ Background scheduler started (minutely check for custom and base times; weekly on Sun 18:00; cleanup at 03:30 in {TARGET_TZ})")

    # Разовая проверка устаревших заданий при старте
    try:
        cleanup_old_tasks(days=30)
    except Exception as cl_err:
        logger.warning(f"Startup task cleanup error: {cl_err}")

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
