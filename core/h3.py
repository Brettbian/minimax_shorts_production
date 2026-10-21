"""MiniMax H3 video generation (v2 API): submit -> poll -> download."""
import base64
import mimetypes
import time
from pathlib import Path

import requests

from . import config

MODERATION_CODE = "1026"  # "contains sensitive content": retrying the same prompt will not help
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


class H3Error(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code

    @property
    def moderation(self) -> bool:
        return self.code == MODERATION_CODE


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"}


def _data_uri(path: Path) -> str:
    media = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{media};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def _api_error(r: requests.Response) -> H3Error:
    try:
        msg = r.json().get("error", {}).get("message", r.text)
    except ValueError:
        msg = r.text
    # MiniMax puts the internal code in trailing parentheses: "... (1026)"
    code = msg.rsplit("(", 1)[-1].rstrip(")") if msg.endswith(")") else None
    return H3Error(f"HTTP {r.status_code}: {msg}", code)


def submit(prompt: str, reference_images: list[Path], resolution: str) -> str:
    """Start a reference-to-video task and return its task_id."""
    body = {
        "model": config.H3_MODEL,
        "content": [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "role": "reference_image", "image_url": {"url": _data_uri(p)}}
            for p in reference_images[:config.MAX_IMAGES]
        ],
        "duration": config.SHOT_SECONDS,
        "resolution": resolution,
        "ratio": config.RATIO,
    }
    r = requests.post(f"{config.API_BASE}/v2/video_generation", headers=_headers(), json=body, timeout=120)
    if r.status_code != 200:
        raise _api_error(r)
    return r.json()["task_id"]


def query(task_id: str) -> dict:
    r = requests.get(f"{config.API_BASE}/v2/query/video_generation/{task_id}", headers=_headers(), timeout=60)
    if r.status_code != 200:
        raise _api_error(r)
    return r.json()["task"]


def wait(task_id: str, timeout: float = config.POLL_TIMEOUT) -> dict | None:
    """Poll until the task finishes. Returns None on timeout so the caller can resume later."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = query(task_id)
        if task["status"] in TERMINAL_STATUSES:
            return task
        time.sleep(config.POLL_SECONDS)
    return None


def download(url: str, dest: Path):
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    dest.write_bytes(r.content)
