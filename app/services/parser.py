from datetime import date, timedelta
from typing import Optional, Dict, Any

WEEKDAYS_RU = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6
}

def get_next_weekday_date(target_weekday: int) -> date:
    today = date.today()
    days_ahead = target_weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)

def parse_homework(text: str) -> Optional[Dict[str, Any]]:
    """
    Интеллектуальный парсер текста домашних заданий.
    Поддерживает разделители (:, —, -), распознает русский и английский язык:
    - 'сегодня' / 'today' -> дедлайн сегодня
    - 'завтра' / 'tomorrow' -> дедлайн завтра (по умолчанию)
    - 'послезавтра' -> дедлайн через 2 дня
    - дни недели ('в понедельник', 'во вторник' и т.д.)
    """
    if not text:
        return None

    separators = [":", "—", "-"]
    today = date.today()

    for sep in separators:
        if sep in text:
            subject_part, description = text.split(sep, 1)
            subject = subject_part.strip()
            description = description.strip()
            if not subject or not description:
                continue

            lower_subj = subject.lower()
            due_date = (today + timedelta(days=1)).isoformat()
            clean_subj = subject

            if "послезавтра" in lower_subj:
                due_date = (today + timedelta(days=2)).isoformat()
                clean_subj = lower_subj.replace("послезавтра", "").strip()
            elif "завтра" in lower_subj or "tomorrow" in lower_subj:
                due_date = (today + timedelta(days=1)).isoformat()
                clean_subj = lower_subj.replace("завтра", "").replace("tomorrow", "").strip()
            elif "сегодня" in lower_subj or "today" in lower_subj:
                due_date = today.isoformat()
                clean_subj = lower_subj.replace("сегодня", "").replace("today", "").strip()
            else:
                for w_name, w_idx in WEEKDAYS_RU.items():
                    if w_name in lower_subj:
                        due_date = get_next_weekday_date(w_idx).isoformat()
                        clean_subj = lower_subj.replace(f"в {w_name}", "").replace(f"во {w_name}", "").replace(w_name, "").strip()
                        break

            clean_subj = clean_subj.lstrip("в ").lstrip("во ").strip()
            if clean_subj:
                clean_subj = clean_subj[0].upper() + clean_subj[1:]
            else:
                clean_subj = subject.strip().title()

            return {
                "subject": clean_subj,
                "description": description,
                "due_date": due_date
            }

    return None

if __name__ == "__main__":
    tests = [
        "Алгебра — упражнения 1-5 на стр. 45",
        "Физика: прочитать параграф 4",
        "История - подготовиться к тесту",
        "Завтра геометрия: номер 10-15",
        "В пятницу химия: выучить формулы",
        "Послезавтра литература: сочинение",
        "Tomorrow math: exercises 10-15",
        "Random hello message without separator"
    ]
    for t in tests:
        result = parse_homework(t)
        print(f"'{t}' -> {result}")
