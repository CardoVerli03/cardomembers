FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY bot.py .

# Expose port (Render sets PORT env var)
EXPOSE 8000

# Start the server
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8000}"]
