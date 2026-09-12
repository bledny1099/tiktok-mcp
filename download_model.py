import sys
from faster_whisper import WhisperModel

print("Downloading and loading model...", flush=True)
try:
    model = WhisperModel(
        "large-v3-turbo",
        device="cpu",
        compute_type="int8",
        download_root="/var/lib/tiktok-mcp/models"
    )
    print("model ready", flush=True)
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
