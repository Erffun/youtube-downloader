FROM brainicism/bgutil-ytdlp-pot-provider:1.3.2-node AS bgutil

FROM node:26-bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 \
    python3-venv \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app.py .

COPY --from=bgutil /app/build /opt/bgutil/build
COPY --from=bgutil /app/node_modules /opt/bgutil/node_modules

EXPOSE 10000

CMD ["sh", "-c", "node /opt/bgutil/build/main.js & exec /opt/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]