<div align="center">

# 🎒 HW Manager • 10-й Класс
### Telegram Bot & Interactive Mini App (WebApp)

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Aiogram](https://img.shields.io/badge/Aiogram-3.x-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://aiogram.dev)
[![Supabase](https://img.shields.io/badge/Supabase-PostgreSQL-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)](https://supabase.com)
[![TailwindCSS](https://img.shields.io/badge/Tailwind-CSS-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)](https://tailwindcss.com)

*Современный интерактивный портал домашних заданий, расписания уроков и обмена учебными материалами для учащихся 10-го класса.*

</div>

---

## 🌟 Ключевые возможности

- 📚 **Единый центр домашних заданий:**
  - Просмотр актуальных заданий по предметам и датам сдачи.
  - Умная группировка («На сегодня», «На завтра», «Срочные», «На неделю»).
  - Отметка выполненного («Сдано») в один клик с мгновенным обновлением прогресс-бара.
- 📸 **Фотоконспекты и материалы доски:**
  - Загрузка фотографий прямо из приложения или камеры смартфона.
  - Автоматическое выравнивание по EXIF и умное сжатие (LANCZOS, качество 80–85%) без потери читаемости формул.
  - Быстрый просмотр и полноэкранный зум.
  - Трехуровневое кэширование: RAM $\to$ Диск (`uploads/`) $\to$ База данных с поддержкой HTTP ETag (`304 Not Modified`).
- 🏆 **Таблица лидеров и геймификация:**
  - Система баллов за активность: создание заданий (+5), прикрепление конспектов (+3), сдача ДЗ (+1).
  - Ранги и бейджи: *«Главный редактор»*, *«Супер-активист»*, *«Мастер конспектов»*, *«Архивариус»*.
  - Автоматическое кэширование рейтинга для снижения нагрузки на БД.
- ⏰ **Персональные уведомления и расписание:**
  - Гибкая настройка часов напоминаний (по умолчанию: 08:00, 16:00, 18:00).
  - Поддержка произвольных пользовательских таймингов (например, `19:30` или `21:00`) через команду `/addtime`.
  - Еженедельный дайджест в воскресенье в 18:00 со всеми заданиями на предстоящую неделю.
  - Корректная работа с часовым поясом (`Asia/Tashkent`).
- 🌐 **Единая база пользователей (`global_users`):**
  - Автоматическая безопасная синхронизация новых пользователей с центральной таблицей Supabase.
  - Валидация контактов, имени и фамилии с учетом регулярных выражений и структуры JSONB.

---

## 🏗️ Архитектура системы

```
                              ┌───────────────────────────────────┐
                              │    Telegram Client (iOS / Android) │
                              └─────────────────┬─────────────────┘
                                                │
                     ┌──────────────────────────┴──────────────────────────┐
                     ▼                                                     ▼
        ┌─────────────────────────┐                           ┌─────────────────────────┐
        │  Telegram Bot (Aiogram) │                           │  Telegram Mini App (UI) │
        │  • Команды: /start, /top│                           │  • SPA на чистом JS     │
        │  • Напоминания (Cron)   │                           │  • Tailwind CSS         │
        │  • Профиль & Меню       │                           │  • Canvas-оптимизация   │
        └────────────┬────────────┘                           └────────────┬────────────┘
                     │                                                     │
                     └──────────────────────────┬──────────────────────────┘
                                                ▼
                               ┌─────────────────────────────────┐
                               │     FastAPI Backend Engine      │
                               │  • REST API: /api/tasks, upload │
                               │  • ETag & Image Optimization    │
                               │  • Task Auto-Cleanup Loop       │
                               └────────────────┬────────────────┘
                                                │
                      ┌─────────────────────────┴─────────────────────────┐
                      ▼                                                   ▼
       ┌──────────────────────────────┐                   ┌──────────────────────────────┐
       │   Основная БД (Supabase)     │                   │   Локальный фолбэк (SQLite)  │
       │   • PostgreSQL + RLS         │                   │   • homework.db              │
       │   • users, tasks, completions│                   │   • Автоматическая миграция  │
       │   • task_materials, prefs    │                   │   • Работа при сбое сети     │
       └──────────────┬───────────────┘                   └──────────────────────────────┘
                      │
                      ▼
       ┌──────────────────────────────┐
       │   Глобальная база (Supabase) │
       │   • public.global_users      │
       │   • Проект HWManager_byTheKing│
       └──────────────────────────────┘
```

---

## 📁 Структура проекта

```
HWManager_byTheKing/Original/
├── .github/
│   └── workflows/
│       └── keep_alive.yml     # Cron-воркер для поддержания инстанса на бесплатном Render
├── static/
│   ├── index.html             # Главный интерфейс WebApp (SPA)
│   └── schedule_10a.jpg       # Базовое изображение расписания уроков
├── uploads/                   # Локальный дисковый кэш оптимизированных фото (.gitkeep)
├── bot.py                     # Telegram-бот на Aiogram 3.x (хендлеры, меню, рассылки)
├── server.py                  # FastAPI бэкенд (REST API, обработка фото, кэширование)
├── run.py                     # Единая точка запуска (FastAPI + Polling + Scheduler + Keep-Alive)
├── db.py                      # Двухуровневый адаптер базы данных (SQLite fallback)
├── supabase_db.py             # Клиент облачной БД Supabase (PostgreSQL + RLS)
├── global_users_service.py    # Сервис фоновой синхронизации с public.global_users
├── launch_services.py         # Скрипт локальной разработки (Uvicorn + Cloudflare Tunnel + Bot)
├── parser.py                  # Парсер текста домашних заданий (RU / EN)
├── requirements.txt           # Зависимости Python
├── Dockerfile                 # Сборка контейнера для продакшена
├── docker-compose.yml         # Локальный запуск в изолированном контейнере
├── render.yaml                # Конфигурация деплоя на платформу Render.com
├── Procfile                   # Команда запуска для PaaS хостингов
├── supabase_schema.sql        # Полный DDL-скрипт инициализации таблиц Supabase
├── CHANGELOG.md               # Подробный журнал всех изменений и исправлений
└── README.md                  # Документация проекта
```

---

## 🚀 Быстрый старт

### 1. Клонирование и окружение
```bash
git clone https://github.com/SwagaWaga/HW_MANAGER.git
cd HW_MANAGER
python -m venv .venv

# Для Windows:
.venv\Scripts\activate
# Для Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Настройка переменных окружения (`.env`)
Создайте файл `.env` в корне проекта:
```ini
# Токен Telegram-бота из @BotFather
BOT_TOKEN=123456789:ABCDefghIJKlmNoPQRsTUVwxyZ

# Публичный адрес развернутого WebApp (или Cloudflare Tunnel адрес)
WEB_APP_URL=https://your-domain.onrender.com

# Параметры подключения к вашей основной базе Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-supabase-anon-or-service-key

# Часовой пояс (по умолчанию: Asia/Tashkent)
BOT_TIMEZONE=Asia/Tashkent

# Опционально: секретный ключ администратора для ручной очистки архива
ADMIN_SECRET_KEY=super-secret-admin-pass
```

### 3. Запуск

#### Вариант А: Продакшен запуск (Все сервисы в одном процессе)
```bash
python run.py
```
*Запускает FastAPI на порту 8000, фоновый Telegram Polling и поминутный планировщик напоминаний.*

#### Вариант Б: Локальная разработка с внешним туннелем Cloudflare
```bash
python launch_services.py
```
*Запустит локальный Uvicorn, поднимет публичный https-туннель через `cloudflared` и запустит бота с активной ссылкой на туннель.*

#### Вариант В: Запуск в Docker
```bash
docker compose up -d --build
```

---

## 🔒 Безопасность и права доступа

1. **Разграничение прав (RBAC):**
   - Ученик (`student`) может редактировать и удалять **только свои задания**.
   - Редактирование и удаление чужих заданий разрешено только пользователям с ролями `admin`, `teacher` или `moderator`.
2. **Защита от Path Traversal:**
   - Эндпоинт загрузки и отдачи медиафайлов строго изолирован каталогом `uploads/`. Попытки выхода за пределы папки пресекаются нормализацией путей.
3. **Безопасная автоочистка:**
   - Очистка устаревших заданий имеет нижний предел (`days >= 14`), исключающий случайное удаление актуальных заданий. Эндпоинт защищается проверкой `ADMIN_SECRET_KEY`.

---

## 📖 Журнал изменений
Подробную историю релизов, исправлений багов и добавления новых возможностей смотрите в файле [CHANGELOG.md](CHANGELOG.md).
