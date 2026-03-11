#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

cd "${REPO_ROOT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [[ ! -f "${REPO_ROOT}/oapi-sdk-python/setup.py" ]]; then
  echo "oapi-sdk-python is missing. If you cloned from git, run: git submodule update --init --recursive" >&2
  exit 1
fi

python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r "${REPO_ROOT}/requirements.txt"
python -m pip install --no-build-isolation "${REPO_ROOT}/oapi-sdk-python"

if [[ ! -f "${REPO_ROOT}/.env" && -f "${REPO_ROOT}/.env.example" ]]; then
  cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
  echo "Created ${REPO_ROOT}/.env from .env.example"
fi

echo
echo "Deploy complete."
echo "Next steps:"
echo "1. Edit ${REPO_ROOT}/.env"
echo "2. Set APP_ID, APP_SECRET, and CODEX_WORKSPACE"
echo "3. Start the bot with: source ${VENV_DIR}/bin/activate && python ${REPO_ROOT}/feishu_codex_bot.py"
