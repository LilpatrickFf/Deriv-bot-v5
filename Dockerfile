# Use Python 3.11 (stable / compatible with aiohttp wheels)
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install minimal build deps (kept small so pip can build optional wheels if needed)
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install dependencies first (cache layer)
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY . /app

# Expose port (optional, Render provides PORT env)
ENV PORT=${PORT:-8080}

# Start the app
CMD ["python", "app.py"]
