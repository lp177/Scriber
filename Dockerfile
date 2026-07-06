# --- Stage 1: build the Vue admin dashboard ---
FROM node:22-alpine AS frontend
WORKDIR /build
# No lockfile is committed, so use a plain npm install.
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.12-slim
# libopus0 is required by discord.py voice support.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
# `davey` (pulled in by discord.py[voice]) must stay installed — discord.py 2.7
# refuses to connect to voice without it, and its DAVE MLS session is what
# scriber.bot.client uses to decrypt received voice E2EE audio so voice_recv can
# decode it. See the note there and in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt
COPY scriber/ ./scriber/
COPY --from=frontend /build/dist ./frontend/dist
ENV SCRIBER_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "scriber"]
