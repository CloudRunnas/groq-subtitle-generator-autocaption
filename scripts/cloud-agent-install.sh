#!/usr/bin/env bash
# Idempotent dependency setup for Cloud Agents.
# Installs frontend (npm) and backend (Python venv with CPU-only torch + WhisperX) deps.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Ensuring system dependencies (ffmpeg, python venv)"
NEED_APT=0
command -v ffmpeg >/dev/null 2>&1 || NEED_APT=1
python3 -c 'import ensurepip' >/dev/null 2>&1 || NEED_APT=1
if [ "$NEED_APT" = "1" ]; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends ffmpeg python3-venv
fi
ffmpeg -version | head -1

echo "==> Installing frontend dependencies (npm ci)"
npm ci

echo "==> Setting up backend Python virtual environment"
cd backend
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

python -m pip install --upgrade pip

echo "==> Installing CPU-only torch / torchaudio"
pip install \
  torch==2.8.0+cpu torchaudio==2.8.0+cpu \
  --index-url https://download.pytorch.org/whl/cpu

echo "==> Installing remaining backend requirements"
grep -vE '^(torch|torchaudio)\b' requirements.txt > /tmp/requirements.notorch.txt
pip install -c constraints-docker.txt \
  -r /tmp/requirements.notorch.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu

# Test-only dependency used by the backend test suite / CI.
pip install pytest

cd "$REPO_ROOT"

echo "==> Ensuring backend/.env exists"
if [ ! -f backend/.env ]; then
  cat > backend/.env << 'EOF'
# Groq API Configuration (replace with a real key for transcription/translation)
GROQ_API_KEY=your_groq_api_key_here

# Model Configuration
GROQ_MODEL=qwen/qwen3-32b
GROQ_WHISPER_MODEL=whisper-large-v3

# Local development storage
STYLE_STORAGE=local
CORS_ORIGINS=http://localhost:3000
EOF
  echo "Created backend/.env (placeholder GROQ_API_KEY)"
else
  echo "backend/.env already exists"
fi

echo "==> Install complete"
