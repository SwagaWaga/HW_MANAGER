-- ========================================================
-- 🏫 HOMEWORK BOT & WEBAPP: SUPABASE DATABASE SCHEMA
-- ========================================================

-- 1. Таблица пользователей (ученики и учителя школы)
CREATE TABLE IF NOT EXISTS public.users (
    id BIGINT PRIMARY KEY, -- Telegram User ID
    telegram_nick TEXT, -- Telegram username (например, @ivan_petrov)
    full_name TEXT, -- Настоящее ФИО (например, "Иван Петров")
    role TEXT DEFAULT 'student' CHECK (role IN ('student', 'teacher', 'admin', 'moderator')),
    class_name TEXT DEFAULT '10-А', -- Класс (например, 10-А, 10-Б)
    is_verified BOOLEAN DEFAULT TRUE, -- Флаг верификации (для закрытого доступа школы)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Таблица домашних заданий
CREATE TABLE IF NOT EXISTS public.tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL, -- Автор задания
    subject TEXT NOT NULL, -- Предмет (Алгебра, Физика, Химия и т.д.)
    description TEXT NOT NULL, -- Описание задания
    due_date DATE NOT NULL, -- Дата сдачи (ГГГГ-ММ-ДД)
    class_name TEXT DEFAULT '10-А', -- Для какого класса
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Таблица выполненных заданий учениками (отметка "Сдано")
CREATE TABLE IF NOT EXISTS public.user_completions (
    user_id BIGINT REFERENCES public.users(id) ON DELETE CASCADE,
    task_id BIGINT REFERENCES public.tasks(id) ON DELETE CASCADE,
    completed_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, task_id)
);

-- 4. Таблица прикрепленных материалов (фото учебника, страниц)
CREATE TABLE IF NOT EXISTS public.task_materials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id BIGINT REFERENCES public.tasks(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL, -- Путь к файлу или URL
    caption TEXT, -- Подпись
    uploader_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы для ускорения выборок
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON public.tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_class ON public.tasks(class_name);
CREATE INDEX IF NOT EXISTS idx_completions_user ON public.user_completions(user_id);
CREATE INDEX IF NOT EXISTS idx_materials_task ON public.task_materials(task_id);

-- Включение Row Level Security (RLS) для защиты данных
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_completions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.task_materials ENABLE ROW LEVEL SECURITY;

-- Публичные политики для анонимного и сервисного доступа через API
CREATE POLICY "Allow select on users" ON public.users FOR SELECT USING (true);
CREATE POLICY "Allow insert/update on users" ON public.users FOR ALL USING (true);

CREATE POLICY "Allow all on tasks" ON public.tasks FOR ALL USING (true);
CREATE POLICY "Allow all on completions" ON public.user_completions FOR ALL USING (true);
CREATE POLICY "Allow all on materials" ON public.task_materials FOR ALL USING (true);
