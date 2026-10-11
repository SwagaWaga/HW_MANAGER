from datetime import date, timedelta  # e.g. "2026-10-09"

def parse_homework(text: str) -> dict | None:
    separators = [":", "—", "-"]
    today = date.today().isoformat()                      # e.g. "2026-10-08"
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    
    for sep in separators:
        if sep in text:
            subject, description = text.split(sep, 1)
            subject = subject.strip()
            description = description.strip()
            raw_subject = subject.strip()
            lower_subject = raw_subject.lower()

            if "tomorrow" in lower_subject:
                due_date = (date.today() + timedelta(days=1)).isoformat()
                # Remove the word 'tomorrow' from the subject and capitalize it:
                clean_subject = lower_subject.replace("tomorrow", "").strip().title()
            elif "today" in lower_subject:
                due_date = date.today().isoformat()
                clean_subject = lower_subject.replace("today", "").strip().title()
            else:
                # Default: if no date was mentioned, assume tomorrow!
                due_date = (date.today() + timedelta(days=1)).isoformat()
                clean_subject = raw_subject.title()
            return {
                "subject": subject,
                "description": description,
                "due_date": due_date
            }
            
    return None


if __name__ == "__main__":
    tests = [
        "Math — exercises 1-5 on page 45",
        "Physics: read chapter 4",
        "History - study for quiz",
        "Random hello message",
        "Tomorrow math: exercises 10-15"
    ]
    for t in tests:
        result = parse_homework(t)
        print(f"Input: '{t}' -> Parsed: {result}")
