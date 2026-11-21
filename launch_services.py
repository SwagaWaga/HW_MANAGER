import subprocess
import sys
import os
import time
import re

DETACHED = 0x00000008 | 0x00000200 # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

base_dir = os.path.abspath(os.path.dirname(__file__))
venv_python = os.path.join(base_dir, '.venv', 'Scripts', 'python.exe')
cloudflared_exe = os.path.join(base_dir, 'cloudflared.exe')

# 1. Kill any lingering python on port 8000 or cloudflared
os.system("taskkill /F /IM cloudflared.exe 2>nul")

# 2. Launch Uvicorn
print("Starting Uvicorn...")
uvicorn_proc = subprocess.Popen(
    [venv_python, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8000"],
    cwd=base_dir,
    creationflags=DETACHED,
    stdout=open(os.path.join(base_dir, "uvicorn.log"), "w", encoding="utf-8"),
    stderr=subprocess.STDOUT
)
print("Uvicorn PID:", uvicorn_proc.pid)

# Give uvicorn a second
time.sleep(1.5)

# Clear tunnel.log
tunnel_log_path = os.path.join(base_dir, "tunnel.log")
with open(tunnel_log_path, "w", encoding="utf-8") as f:
    f.write("")

# 3. Launch Cloudflared
print("Starting Cloudflared...")
cloudflared_proc = subprocess.Popen(
    [cloudflared_exe, "tunnel", "--url", "http://127.0.0.1:8000", "--protocol", "http2", "--logfile", tunnel_log_path],
    cwd=base_dir,
    creationflags=DETACHED
)
print("Cloudflared PID:", cloudflared_proc.pid)

# 4. Wait for tunnel URL
tunnel_url = None
print("Waiting for tunnel URL...")
for i in range(25):
    time.sleep(1)
    if os.path.exists(tunnel_log_path):
        with open(tunnel_log_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r'https://[a-zA-Z0-9\.\-]+trycloudflare\.com', content)
            if matches:
                tunnel_url = matches[-1]
                break

if not tunnel_url:
    print("FAILED to obtain tunnel URL")
    sys.exit(1)

print("OBTAINED TUNNEL URL:", tunnel_url)

# 5. Prepare Bot environment with live tunnel URL
bot_env = os.environ.copy()
bot_env["WEB_APP_URL"] = tunnel_url

# 6. Launch Telegram Bot
print("Starting Telegram Bot...")
bot_proc = subprocess.Popen(
    [venv_python, "bot.py"],
    cwd=base_dir,
    env=bot_env,
    creationflags=DETACHED,
    stdout=open(os.path.join(base_dir, "bot.log"), "w", encoding="utf-8"),
    stderr=subprocess.STDOUT
)
print("Bot PID:", bot_proc.pid)

# 7. Write active URL to a persistent file
with open(os.path.join(base_dir, "ACTIVE_URL.txt"), "w", encoding="utf-8") as f:
    f.write(tunnel_url)

print("=== ALL SERVICES STARTED SUCCESSFULLY ===")
print("LIVE URL:", tunnel_url)
