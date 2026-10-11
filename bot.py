import os
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message
from db import init_db, get_pending_tasks, add_task, mark_task_done, delete_task, get_today_tasks
from datetime import date

logging.basicConfig(level=logging.INFO)

# Loads the secret token from env
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
    await message.answer(f"✅ Added #{task_id}: [{subject}] {description} (Due: {due_date})")

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

@dp.message(Command("pending"))
async def handle_pending(message: Message):
    user_id = message.from_user.id
    tasks = get_pending_tasks(user_id)
    if not tasks:
        await message.answer("You have no pending tasks.")
    else:
        response = "Your pending tasks:\n"
        for task in tasks:
            response += f"- #{task['id']} [{task['subject']}]: {task['description']} (Due: {task['due_date']})\n"
        await message.answer(response)

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

if __name__ == '__main__':
    init_db()  # Initialize the database
    asyncio.run(dp.start_polling(bot))
