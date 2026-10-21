"""Settings shared by the whole pipeline. Secrets come from .env (or Streamlit secrets); the rest is here."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("MINIMAX_API_KEY", "")
# Keys are region-bound: international keys work on api.minimax.io, China keys on api.minimaxi.com.
API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimax.io")

M3_MODEL = "MiniMax-M3.1-Flash"
M3_EFFORT = "medium"      # M3.1-Flash always thinks; effort trades depth for latency (low ... max)
H3_MODEL = "MiniMax-H3-Max"
RESOLUTIONS = ("480P", "768P")  # H3-Max options: 480P to iterate cheaply, 768P for demos

# Fixed POC format: 3 shots x 5s, vertical. H3-Max supports 5-15s per clip.
SHOT_COUNT = 3
SHOT_SECONDS = 5
RATIO = "9:16"
MAX_IMAGES = 9            # H3 accepts at most 9 reference images

AUTO_RETRIES = 1          # automatic re-generations after a failed video QC
POLL_SECONDS = 10         # H3 docs recommend ~10s polling
POLL_TIMEOUT = 15 * 60    # stop waiting after this; the shot stays "submitted" and resume re-polls it

OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
CROSSFADE = 0.3           # seconds of video/audio crossfade between shots

DATA_DIR = ROOT / "data" / "projects"
SAMPLES_DIR = ROOT / "samples"

# Bold Chinese caption font as (file, face index): FONT_PATH wins, else the first one found
# (macOS Hiragino W6, then Debian fonts-noto-cjk where face 2 is Simplified Chinese).
FONT_CANDIDATES = [
    (os.environ.get("FONT_PATH", ""), 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1),
]


def caption_font() -> tuple[str, int]:
    for path, index in FONT_CANDIDATES:
        if path and Path(path).exists():
            return path, index
    raise RuntimeError("No Chinese font found; set FONT_PATH in .env")
