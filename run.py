import asyncio
import os
import sys
import logging
import uvicorn
from contextlib import asynccontextmanager
from server import app
from bot import dp, bot, send_evening_reminder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from db import init_db

# Flush stdout/stderr immediately for real-time Render and container logs
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hw_runner")

scheduler = AsyncIOScheduler()

async def render_keep_alive_task():
    """
    Self-pinger to prevent free hosting (such as Render.com) from spinning down
    after 15 minutes of inactivity. Pings every 10 minutes.
    """
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEB_APP_URL")
    if not render_url or "localhost" in render_url or "127.0.0.1" in render_url:
        return
    ping_url = render_url.rstrip("/") + "/ping"
    logger.info(f"🔄 Keep-Alive self-pinger active for: {ping_url}")
    await asyncio.sleep(45)
    while True:
        try:
            import urllib.request
            req = urllib.request.Request(ping_url, headers={"User-Agent": "HW-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                pass
        except Exception as e:
            logger.debug(f"Keep-alive ping note: {e}")
        await asyncio.sleep(600)

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

    # 2. Start Telegram Bot polling in the background (if BOT_TOKEN is set)
    bot_task = None
    if bot:
        bot_task = asyncio.create_task(dp.start_polling(bot))
        logger.info("🤖 Telegram Bot polling started in background")
    else:
        logger.warning("⚠️ BOT_TOKEN is not configured! Telegram Bot polling is disabled. (Set BOT_TOKEN in Render Environment Variables)")

    # 3. Start Keep-Alive task
    pinger_task = asyncio.create_task(render_keep_alive_task())

    try:
        yield
    finally:
        logger.info("🛑 Shutting down services...")
        pinger_task.cancel()
        if bot_task:
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass
        if bot and hasattr(bot, 'session') and bot.session:
            await bot.session.close()
        scheduler.shutdown(wait=False)
        logger.info("👋 All services stopped cleanly")

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(f"🌐 Serving on {host}:{port}")
    uvicorn.run("run:app", host=host, port=port, reload=False)
