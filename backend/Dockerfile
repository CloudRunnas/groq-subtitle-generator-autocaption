# Backend image for ECS Fargate (WhisperX + FFmpeg, CPU torch only)
# Build: docker buildx build --load -f backend/Dockerfile -t autocaption-backend .
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS deps
COPY backend/requirements.txt /app/requirements.txt
COPY backend/constraints-docker.txt /app/constraints-docker.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        torch==2.8.0+cpu torchaudio==2.8.0+cpu \
        --index-url https://download.pytorch.org/whl/cpu \
    && grep -vE '^(torch|torchaudio)\b' /app/requirements.txt > /app/requirements.notorch.txt \
    && pip install --no-cache-dir -c /app/constraints-docker.txt \
        -r /app/requirements.notorch.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu

FROM base AS runtime
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY backend /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    STYLE_STORAGE=s3 \
    MEDIA_LOCAL_DIR=/tmp/media \
    STYLE_LOCAL_DIR=/tmp/styles

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
