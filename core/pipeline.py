"""Per-shot state machine. `run` is both "start" and "resume": it moves every shot forward from whatever
state.json says, so a crash or a closed browser loses nothing. Shots are independent, so they run in parallel.

  pending ──submit──> submitted ──poll──> generated ──QC──> qc_passed
     ^                    │                    │
     │                  failed              qc_failed ──(auto-retry budget left)──┐
     └────────────────────────────────────────────────────────────────────────────┘

Each step saves state.json before returning, and returns True to keep driving the shot or False to stop.
"""
import queue
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait

import requests

from . import config, h3, qc, storyboard
from .project import Project, ShotState, State, Storyboard

Log = Callable[[str], None]


def approve(project: Project, resolution: str = config.RESOLUTIONS[0], edited: Storyboard | None = None) -> State:
    """User confirmation: lock the storyboard (with the editor's last edits, if given) and start a fresh state.

    Writes go dependents-first so a crash in between is always safe: the new state is written before the
    storyboard is marked approved, and nothing runs from an unapproved storyboard."""
    with project.lock():
        sb = project.load_storyboard()
        if sb is None:
            raise RuntimeError("还没有分镜")
        if edited is not None:
            storyboard.assert_current(project, edited)
            sb = edited
        elif sb.approved:
            raise RuntimeError("分镜已经确认过了；要从头再来，请重新生成分镜")
        st = State(resolution=resolution, shots=[ShotState() for _ in sb.shots])
        project.save_state(st)
        sb.approved = True
        project.save_storyboard(sb)
        return st


def _load(project: Project) -> tuple[Storyboard, State]:
    sb, st = project.load_storyboard(), project.load_state()
    if sb is None or not sb.approved or st is None:
        raise RuntimeError("请先确认分镜，再生成视频")
    return sb, st


def run(project: Project, log: Log = print) -> State:
    """Start or resume every unfinished shot."""
    with project.lock():
        sb, st = _load(project)
        return _drive(project, sb, st, range(len(st.shots)), log)


def retry_shot(project: Project, index: int, *, visual: str | None = None, camera: str | None = None,
               sound: str | None = None, log: Log = print) -> State:
    """Manual per-shot retry: apply any edits to the shot, reset it, and drive only that shot."""
    with project.lock():
        sb, st = _load(project)
        # Reset the shot (and drop the final) before saving the edits: a crash in between then leaves a pending
        # shot that regenerates, never an old video standing in for the edited shot.
        st.shots[index] = ShotState(attempt=st.shots[index].attempt)
        st.final = None
        project.save_state(st)
        edits = {k: v for k, v in {"visual": visual, "camera": camera, "sound": sound}.items() if v is not None}
        if edits:
            sb.shots[index] = sb.shots[index].model_copy(update=edits)
            project.save_storyboard(sb)
        log(f"镜头 {index + 1}：已重置，重新生成")
        return _drive(project, sb, st, [index], log)


def edit_caption(project: Project, index: int, caption: str):
    """Captions are only burned in at assembly, so changing one needs no regeneration, just a new final.
    The final is dropped before the caption is saved, so a crash in between can't leave a final with old text."""
    with project.lock():
        sb, st = _load(project)
        st.final = None
        project.save_state(st)
        sb.shots[index].caption = caption
        project.save_storyboard(sb)


def _drive(project: Project, sb: Storyboard, st: State, indices, log: Log) -> State:
    """Drive the given shots in parallel. Workers only queue log lines; `log` is called from this thread
    (Streamlit elements can't be written from worker threads). The caller holds the project lock."""
    lines: queue.Queue[str] = queue.Queue()
    shots = _Shots(project, sb, st, lines.put)

    def flush():
        while not lines.empty():
            log(lines.get())

    with ThreadPoolExecutor(max_workers=len(st.shots)) as pool:
        futures = [pool.submit(shots.advance, i) for i in indices]
        while wait(futures, timeout=0.5).not_done:
            flush()
    flush()
    for f in futures:
        f.result()  # re-raise anything unexpected from a worker
    return st


class _Shots:
    """The shared state of one `run`: each worker thread drives one shot; saves are serialized."""

    def __init__(self, project: Project, sb: Storyboard, st: State, log: Log):
        self.project, self.sb, self.st, self.log = project, sb, st, log
        self._lock = threading.Lock()

    def save(self):
        with self._lock:
            self.project.save_state(self.st)

    def advance(self, i: int):
        """Drive shot i until it reaches a resting state (qc_passed, qc_failed, or a stop on error)."""
        steps = {"pending": self._submit, "failed": self._submit, "submitted": self._collect, "generated": self._review}
        while (step := steps.get(self.st.shots[i].status)) and step(i):
            pass

    def _submit(self, i: int) -> bool:
        s = self.st.shots[i]
        if s.moderation:
            self.log(f"镜头 {i + 1}：内容审核未通过，请修改画面后重试")
            return False
        s.prompt = storyboard.h3_prompt(self.sb, i, s.retry_hint)
        try:
            s.task_id = h3.submit(s.prompt, self.project.images, self.st.resolution)
        except (h3.H3Error, requests.RequestException) as e:
            s.status, s.error = "failed", str(e)
            s.moderation = isinstance(e, h3.H3Error) and e.moderation
            self.save()
            self.log(f"镜头 {i + 1}：提交失败：{e}")
            return False
        s.attempt += 1
        s.status, s.error = "submitted", None
        self.save()  # task_id is on disk before we wait, so resume re-polls instead of paying again
        self.log(f"镜头 {i + 1}：已提交 H3（第 {s.attempt} 次）")
        return True

    def _collect(self, i: int) -> bool:
        s = self.st.shots[i]
        try:
            task = h3.wait(s.task_id)
        except (h3.H3Error, requests.RequestException) as e:
            self.log(f"镜头 {i + 1}：查询进度出错（{e}），稍后点“继续”即可接着查询")
            return False
        if task is None:
            self.log(f"镜头 {i + 1}：还在生成中，稍后点“继续”即可接着查询")
            return False
        if task["status"] != "succeeded":
            err = task.get("error") or {}
            s.status, s.error = "failed", f"{task['status']}: {err.get('message', '')}"
            s.moderation = str(err.get("code")) == h3.MODERATION_CODE
            self.save()
            self.log(f"镜头 {i + 1}：H3 生成失败：{s.error}")
            return False
        video = f"shots/shot{i + 1}_try{s.attempt}.mp4"
        try:
            h3.download(task["content"]["url"], self.project.dir / video)
        except requests.RequestException as e:  # the URL may have expired; re-querying gives a fresh one
            self.log(f"镜头 {i + 1}：下载失败（{e}），稍后点“继续”会重新获取")
            return False
        s.video, s.status = video, "generated"
        self.save()
        self.log(f"镜头 {i + 1}：视频已生成，M3 质检中…")
        return True

    def _review(self, i: int) -> bool:
        s = self.st.shots[i]
        s.qc = qc.review(self.project, self.sb, i, self.project.dir / s.video)
        issues = "；".join(s.qc.get("issues") or [])
        if s.qc["pass"]:
            s.status = "qc_passed"
            self.log(f"镜头 {i + 1}：质检通过")
        elif s.auto_retries < config.AUTO_RETRIES:
            s.auto_retries += 1
            s.retry_hint = s.qc.get("retry_hint") or None
            s.status = "pending"
            self.log(f"镜头 {i + 1}：质检未通过（{issues}），带着修正提示自动重试")
        else:
            s.status = "qc_failed"
            self.log(f"镜头 {i + 1}：再次未通过（{issues}），交给你决定")
        self.save()
        return s.status == "pending"
