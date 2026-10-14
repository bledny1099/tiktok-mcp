import sys
from tiktok_mcp import CFG, _get_model

print(
    f"Downloading and loading Whisper model {CFG.whisper_model!r} into {CFG.work_dir / 'models'}...",
    flush=True,
)
try:
    _get_model()
    print("Model ready.", flush=True)
except Exception:
    import traceback

    traceback.print_exc()
    sys.exit(1)
