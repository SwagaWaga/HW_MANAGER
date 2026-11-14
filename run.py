import asyncio
import os
import logging
import uvicorn
from contextlib import asynccontextmanager
from server import app
from bot import dp, bot, send_evening_reminder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hw_runner")

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(fastapi_app):
    logger.info("🚀 Starting 10th Grade Homework Portal & Bot...")
    init_db()

    # 1. Start evening reminder scheduler
    try:
        scheduler.add_job(send_evening_reminder, 'cron', hour=18, minute=0)
        scheduler.start()
        logger.info("⏰ Scheduler started (Evening reminder at 18:00)")
    except Exception as e:
        logger.warning(f"Scheduler note: {e}")

    # 2. Start Telegram Bot polling in the background
    bot_task = asyncio.create_task(dp.start_polling(bot))
    logger.info("🤖 Telegram Bot polling started in background")

    try:
        yield
    finally:
        logger.info("🛑 Shutting down services...")
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        scheduler.shutdown(wait=False)
        logger.info("👋 All services stopped cleanly")

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(f"🌐 Serving on {host}:{port}")
    uvicorn.run("run:app", host=host, port=port, reload=False)
