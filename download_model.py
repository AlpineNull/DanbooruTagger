from pathlib import Path
from huggingface_hub import snapshot_download

APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "models" / "pixai-tagger-v1.0"
REPO_ID = "pixai-labs/pixai-tagger-v1.0"
REVISION = "f33cfdb53c0c90b049bab9ce066eea1118970ef8"

MODEL_DIR.mkdir(parents=True, exist_ok=True)

print("Pobieram PixAI Tagger v1.0 (~2 GB). To jest jednorazowe.")
snapshot_download(
    repo_id=REPO_ID,
    revision=REVISION,
    local_dir=str(MODEL_DIR),
    allow_patterns=[
        "config.json",
        "model.safetensors",
        "preprocessor_config.json",
        "tagger_pipeline.py",
    ],
)

required = [
    MODEL_DIR / "config.json",
    MODEL_DIR / "model.safetensors",
    MODEL_DIR / "preprocessor_config.json",
    MODEL_DIR / "tagger_pipeline.py",
]
missing = [str(p.name) for p in required if not p.exists()]
if missing:
    raise SystemExit("Brakuje plików modelu: " + ", ".join(missing))

print("Model gotowy.")
