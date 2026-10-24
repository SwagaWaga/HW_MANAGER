import os
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from db import init_db, get_pending_tasks, add_task, mark_task_done, delete_task, get_today_tasks, get_overdue_tasks, delete_all_tasks
from datetime import date, timedelta
from parser import parse_homework
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)

# Loads the secret token from env
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def send_evening_reminder():
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    user_id = 5783100321
    tasks = get_today_tasks(user_id, tomorrow_str)
    
    if tasks:
        text = "🔔 Evening Reminder! Here is what's due tomorrow:\n"
        for t in tasks:
            text += f"- [{t['subject']}]: {t['description']}\n"
        await bot.send_message(chat_id=user_id, text=text)

@dp.callback_query()
async def handle_done_callback(callback: CallbackQuery):
    if callback.data.startswith("done:"):
        task_id = int(callback.data.split(":")[1])
        mark_task_done(task_id, callback.from_user.id)
        await callback.answer("Task completed! 🎉")
        await callback.message.edit_text(f"Task #{task_id} completed! ✅")

@dp.message(CommandStart())
async def handle_start(message: Message):
    await message.answer('Hi!')

@dp.message(Command("add"))
async def handle_add(message :Message, command: CommandObject):
    user_id = message.from_user.id

    if not command.args:
        await message.answer("Usage: /add <subject> <YYYY-MM-DD> <task description>\nExample: /add Math 2026-10-05 Page 42 ex 1-5")
        return
    
    args = command.args.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("Usage: /add <subject> <YYYY-MM-DD> <task description>")
        return

    subject = args[0]
    due_date = args[1]
    description = args[2]

    task_id = add_task(message.from_user.id, subject, description, due_date)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Mark Done", callback_data=f"done:{task_id}")]])

    await message.answer(
    f"✅ Added #{task_id}: [{subject}] {description} (Due: {due_date})",
    reply_markup=keyboard)

@dp.message(Command("done"))
async def handle_done(message: Message, command: CommandObject):
    user_id = message.from_user.id

    if not command.args or not command.args.isdigit():
        await message.answer("Usage: /done <task_id>\nExample: /done 2")
        return

    task_id = int(command.args)
    mark_task_done(task_id, user_id)
    await message.answer(f"🎉 Great job! Marked task #{task_id} as done.")

@dp.message(Command("delete"))
async def handle_delete(message: Message, command: CommandObject):
        user_id = message.from_user.id

        if not command.args or not command.args.isdigit():
            await message.answer("Usage: /delete <task_id>\nExample: /delete 2")
            return

        task_id = int(command.args)
        delete_task(task_id, user_id)
        await message.answer(f"🗑️ Deleted task #{task_id}.")

@dp.message(Command("delete_all"))
async def handle_delete_all(message: Message):
        user_id = message.from_user.id
        delete_all_tasks(user_id)

        await message.answer("🗑️ All tasks successfully deleted")

@dp.message(Command("today"))
async def handle_today_tasks(message: Message):
    today_str = date.today().isoformat()
    user_id = message.from_user.id
    tasks = get_today_tasks(user_id, today_str)

    if not tasks:
        await message.answer("🎉 No homework due today! You're free.")
    else:
        response = "Your today tasks:\n"
        for task in tasks:
            response += f"- #{task['id']} [{task['subject']}]: {task['description']} (Due: {task['due_date']})\n"
        await message.answer(response)

@dp.message(Command("overdue"))
async def handle_overdue(message:Message):
    today_str = date.today().isoformat()
    user_id = message.from_user.id
    over_due_tasks = get_overdue_tasks(user_id, today_str)

    if not over_due_tasks :
        await message.answer("No overdue tasks")
    else:
        response = "Your overdue tasks:\n"
        for task in over_due_tasks:
            response += f"- #⚠️{task['id']} [{task['subject']}]: {task['description']} (Due: {task['due_date']})\n"
        await message.answer(response)

@dp.message(Command("priority", "pending"))
async def handle_priority(message:Message):
    user_id = message.from_user.id
    today_str = date.today().isoformat()
    tasks = get_pending_tasks(user_id)
    priority_1 = []
    priority_2 = []
    priority_3 = []

    if not tasks:
        await message.answer("You don't have any tasks to do")
    else:
        response_1 = "Tasks for today:\n"
        response_2 = "Tasks for tommorow:\n"
        response_3 = "Tasks for other days:\n"
        for task in tasks:
            if task['due_date'] <= today_str:
                priority_1.append(task)
            elif task['due_date'] == (date.today() + timedelta(days=1)).isoformat():
                priority_2.append(task)
            else:
                priority_3.append(task)
        for task in priority_1:
            response_1 += (f"- #⚠️{task['id']} [{task['subject']}]: {task['description']}\n")
        for task in priority_2:
            response_2 += (f"- #{task['id']} [{task['subject']}]: {task['description']}\n")
        for task in priority_3:
            response_3 += (f"- #{task['id']} [{task['subject']}]: {task['description']} (Due: {task['due_date']})\n")
        await message.answer(f"{response_1}\n{response_2}\n{response_3}")

@dp.message()
async def handle_text(message:Message):
    parsed = parse_homework(message.text)

    if parsed is not None:
        task_id = add_task(message.from_user.id, parsed['subject'], parsed['description'], parsed['due_date'])
        await message.answer(f"📝 Noted! #{task_id}: [{parsed['subject']}] {parsed['description']} (Due: {parsed['due_date']})")
    else:
        await message.answer("Program have not recognized it as a homework")

async def main():
    init_db()
    # 1. Start our background reminder scheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_evening_reminder, 'cron', hour=18, minute=0)
    scheduler.start()

    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
