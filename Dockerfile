# Portable image: runs the scheduler loop anywhere (Railway, Fly, Render, VPS).
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV STORAGE_BACKEND=file DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["python", "worker.py"]
