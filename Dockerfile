FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .
COPY templates/ templates/

# Writable dirs for uploads and outputs
RUN mkdir -p uploads outputs

EXPOSE 5000

CMD ["python", "app.py"]
