FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr so logs appear immediately in Render
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install FFmpeg (required for audio extraction), curl and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Render assigns a PORT environment variable dynamically (typically 10000)
ENV PORT=10000
EXPOSE 10000

# Start Uvicorn ASGI server binding to 0.0.0.0 and dynamic $PORT
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
