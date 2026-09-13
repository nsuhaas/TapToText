#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate

python -m pip install -r requirements.txt
python -m pip install -r requirements-offline.txt
python - <<'PY'
from pathlib import Path
import whisper

model_dir = Path.home() / ".taptotext" / "models"
model_dir.mkdir(parents=True, exist_ok=True)
whisper.load_model("base", download_root=str(model_dir))
print(f"Base Whisper model is available in {model_dir}")
PY
python taptotext.py --check-offline

cat <<'EOF'

TapToText offline transcription is installed.

Run the widget:
  open /Users/suhaasn/ST/TapToText/TapToText.app

In Settings, use:
  Backend: whisper-cli
  Model: base
EOF
