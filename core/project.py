"""A project is one folder under data/projects/<id>/ holding all inputs and state as plain files.

  product.json     name + selling points + image file names
  input/           copies of the product images
  storyboard.json  M3's storyboard (user edits it, then approves)
  state.json       per-shot generation state; written after every transition so a crash can resume
  shots/           downloaded H3 clips, one file per attempt
  final.mp4        assembled video
"""
import contextlib
import fcntl
import json
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from . import config


class Shot(BaseModel):
    # All text fields are Chinese: the product, the market and the H3 prompt are all Chinese.
    purpose: str          # what the shot does in the ad (钩子 / 卖点 / 收尾)
    selling_point: str    # the selling point it covers, copied from the input
    visual: str           # scene + product action, for H3
    camera: str           # camera move with H3 bracket commands like [推进]
    sound: str            # sound effects matching the action
    caption: str          # subtitle burned in during assembly; 【】 marks the highlighted keyword


class Storyboard(BaseModel):
    appearance: str       # traits shared by every variant in the photos, repeated in every H3 prompt
    variants: str = ""    # how the variants in the photos differ (colour, style, size...); "" for a single variant
    music_style: str      # one music style shared by all shots so cuts sound less jarring
    shots: list[Shot]
    qc: dict | None = None
    approved: bool = False
    revision: int = 0     # bumped on every save, so UI widgets know when the content changed underneath them


ShotStatus = Literal["pending", "submitted", "generated", "qc_passed", "qc_failed", "failed"]
DONE = ("qc_passed", "qc_failed")  # resting states with a usable video; the rest still need `run`


class ShotState(BaseModel):
    status: ShotStatus = "pending"
    attempt: int = 0          # how many times this shot was submitted to H3
    auto_retries: int = 0     # automatic re-generations used since the last manual retry
    task_id: str | None = None
    prompt: str | None = None
    retry_hint: str | None = None  # QC's fix, appended to the next prompt
    video: str | None = None       # path relative to the project folder
    qc: dict | None = None
    error: str | None = None
    moderation: bool = False       # blocked by content moderation: needs an edited shot, not a blind retry


class State(BaseModel):
    resolution: str
    shots: list[ShotState]
    final: str | None = None

    @property
    def all_done(self) -> bool:
        return all(s.status in DONE for s in self.shots)


def normalize_images(files, out_dir: Path) -> list[Path]:
    """Convert uploads (paths or file-like) to RGB JPEGs within H3's limits:
    shortest side >= 256px, aspect ratio 0.4-2.5, at most MAX_IMAGES. Raises ValueError otherwise."""
    paths = []
    for k, f in enumerate(list(files)[:config.MAX_IMAGES], 1):
        label = getattr(f, "name", str(f))
        img = Image.open(f).convert("RGB")
        w, h = img.size
        if min(w, h) < 256:
            raise ValueError(f"{label}: 图片太小（{w}x{h}），最短边至少 256px")
        if not 0.4 <= w / h <= 2.5:
            raise ValueError(f"{label}: 宽高比 {w / h:.2f} 超出 0.4–2.5 范围")
        img.thumbnail((2048, 2048))
        p = out_dir / f"{k}.jpg"
        img.save(p, "JPEG", quality=92)
        paths.append(p)
    return paths


class ProjectBusy(RuntimeError):
    """Another session or process is already generating, retrying or assembling this project."""


class StaleStoryboard(RuntimeError):
    """The storyboard changed (or was confirmed) after this edit was loaded; saving would overwrite newer data."""


def _write_atomic(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Project:
    def __init__(self, pid: str):
        self.id = pid
        self.dir = config.DATA_DIR / pid
        if not (self.dir / "product.json").exists():
            raise FileNotFoundError(f"no project {pid!r} in {config.DATA_DIR}")

    @classmethod
    def create(cls, name: str, selling_points: list[str], images: list[Path], slug: str = "product") -> "Project":
        pid = f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        while (config.DATA_DIR / pid).exists():  # two projects of the same slug within one second
            pid += "x"
        d = config.DATA_DIR / pid
        (d / "input").mkdir(parents=True)
        (d / "shots").mkdir()
        names = []
        for k, src in enumerate(images, 1):
            dst = f"{k}{Path(src).suffix.lower()}"
            shutil.copy(src, d / "input" / dst)
            names.append(dst)
        product = {"name": name, "selling_points": selling_points, "images": names}
        _write_atomic(d / "product.json", json.dumps(product, ensure_ascii=False, indent=2))
        return cls(pid)

    @classmethod
    def from_sample(cls, slug: str) -> "Project":
        src = config.SAMPLES_DIR / slug
        meta = json.loads((src / "product.json").read_text())
        return cls.create(meta["name"], meta["selling_points"], [src / n for n in meta["images"]], slug)

    @staticmethod
    def list_ids() -> list[str]:
        """Newest first. product.json is written once at creation, so its mtime is the creation time."""
        if not config.DATA_DIR.exists():
            return []
        files = [p / "product.json" for p in config.DATA_DIR.iterdir() if (p / "product.json").exists()]
        return [f.parent.name for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)]

    @property
    def label(self) -> str:
        created = time.strftime("%m-%d %H:%M", time.localtime((self.dir / "product.json").stat().st_mtime))
        return f"{self.product['name']} · {created}"

    @property
    def product(self) -> dict:
        return json.loads((self.dir / "product.json").read_text())

    @property
    def images(self) -> list[Path]:
        return [self.dir / "input" / n for n in self.product["images"]]

    def load_storyboard(self) -> Storyboard | None:
        p = self.dir / "storyboard.json"
        return Storyboard.model_validate_json(p.read_text()) if p.exists() else None

    def save_storyboard(self, sb: Storyboard):
        # Continue from the stored revision, not the object's: a freshly generated storyboard starts at 0 and
        # would otherwise reuse an old revision number, and with it the UI's stale widget keys.
        stored = self.load_storyboard()
        sb.revision = max(sb.revision, stored.revision if stored else 0) + 1
        _write_atomic(self.dir / "storyboard.json", sb.model_dump_json(indent=2))

    def load_state(self) -> State | None:
        p = self.dir / "state.json"
        return State.model_validate_json(p.read_text()) if p.exists() else None

    def save_state(self, st: State):
        _write_atomic(self.dir / "state.json", st.model_dump_json(indent=2))

    def clear_state(self):
        """Forget all generation progress. Shot and final files stay on disk but are no longer referenced."""
        (self.dir / "state.json").unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock across processes (browser sessions, the CLI) for anything that reads, changes and writes
        the project's state. Fails fast instead of waiting, so a second click gets a clear message."""
        with open(self.dir / ".lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProjectBusy("这个项目正在被另一个操作处理（生成、重试或合成），请稍后再试") from None
            yield  # closing the file releases the lock
