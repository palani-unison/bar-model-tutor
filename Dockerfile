FROM python:3.14-slim

# ffmpeg muxes the whiteboard video; Piper's espeak data ships inside its wheel.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice model (~63 MB) is gitignored, so fetch it at build time.
RUN python -m piper.download_voices en_US-lessac-medium --download-dir /tmp/voice \
    && mkdir -p renderer \
    && mv /tmp/voice/en_US-lessac-medium.onnx renderer/voice.onnx \
    && mv /tmp/voice/en_US-lessac-medium.onnx.json renderer/voice.onnx.json

COPY . .
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
