import asyncio
import os
import sys
import logging
import uvicorn
from contextlib import asynccontextmanager
from server import app
from bot import dp, bot, send_scheduled_reminder, send_evening_reminder, sync_menu_button, TARGET_TZ
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

scheduler = AsyncIOScheduler(timezone=TARGET_TZ)

async def render_keep_alive_task():
    """
    Self-pinger to prevent free hosting (such as Render.com) from spinning down
    after 15 minutes of inactivity. Pings every 5 minutes.
    """
    await asyncio.sleep(20)
    render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    render_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or (f"https://{render_hostname}" if render_hostname else None)
        or os.getenv("WEB_APP_URL")
    )
    if not render_url or "localhost" in render_url or "127.0.0.1" in render_url:
        logger.info(f"ℹ️ Keep-Alive self-pinger idle (local dev mode or no public URL: {render_url})")
        return

    ping_url = render_url.rstrip("/") + "/ping"
    logger.info(f"🔄 Keep-Alive self-pinger ACTIVE for: {ping_url} (interval: 300s / 5min)")
    while True:
        try:
            import urllib.request
            req = urllib.request.Request(ping_url, headers={"User-Agent": "HW-KeepAlive-Worker/2.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                logger.info(f"⚡ Keep-alive ping sent to {ping_url} -> HTTP {resp.status} (Instance stays awake)")
        except Exception as e:
            logger.warning(f"⚠️ Keep-alive ping note: {e}")
        await asyncio.sleep(300)

@asynccontextmanager
async def lifespan(fastapi_app):
    logger.info("🚀 Starting 10th Grade Homework Portal & Bot...")
    init_db()

    # 1. Start reminder scheduler (every day at 08:00, 16:00, 18:00 local time)
    try:
        scheduler.add_job(send_scheduled_reminder, 'cron', hour='8,16,18', minute=0)
        scheduler.start()
        logger.info(f"⏰ Scheduler started (Reminders at 08:00, 16:00, 18:00 in {TARGET_TZ})")
    except Exception as e:
        logger.warning(f"Scheduler note: {e}")

    # 2. Start Telegram Bot polling in the background (if BOT_TOKEN is set)
    bot_task = None
    if bot:
        async def run_bot_polling():
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await sync_menu_button()
                await dp.start_polling(bot, handle_signals=False)
            except Exception as bot_err:
                logger.warning(f"⚠️ Telegram Bot polling note: {bot_err}")

        bot_task = asyncio.create_task(run_bot_polling())
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
    host = "0.0.0.0"
    logger.info(f"🌐 Serving on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
