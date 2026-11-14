# ========================================================
# 🐳 DOCKERFILE FOR 10th GRADE HOMEWORK BOT & WEBAPP
# ========================================================
FROM python:3.11-slim

WORKDIR /app

# Prevent python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure uploads directory exists
RUN mkdir -p uploads

# Expose default HTTP port
EXPOSE 8000

# Run combined runner (FastAPI WebApp + Telegram Bot)
CMD ["python", "run.py"]
