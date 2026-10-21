"""Offline tests for the pipeline's state machine and assembly. No API calls, no cost.

  .venv/bin/python -m unittest discover tests
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests
from PIL import Image

from core import assemble, config, h3, pipeline, qc, storyboard
from core.project import Project, ProjectBusy, Shot, StaleStoryboard, Storyboard, normalize_images

PASS = {"pass": True, "product_fidelity": 5, "visual_quality": 5, "issues": [], "retry_hint": ""}
FAIL = {"pass": False, "product_fidelity": 2, "visual_quality": 3, "issues": ["鞋子变色了"],
        "retry_hint": "鞋面保持亮红色。"}
OK_TASK = {"status": "succeeded", "content": {"url": "https://example.invalid/v.mp4"}}
QUIET = {"log": lambda *_: None}


def replies(*objs) -> mock._patch:
    """Patch M3's raw text replies, so JSON parsing and schema validation run for real."""
    return mock.patch("core.m3.ask", side_effect=[json.dumps(o, ensure_ascii=False) for o in objs])


def make_clip(dest: Path, audio: bool = True, seconds: float = 5.2):
    """A tiny 480x832 test clip, like H3-Max 480P output."""
    args = ["ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=480x832:rate=24:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=32000"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else []) + ["-shortest", str(dest)]
    subprocess.run(args, check=True)


def fake_storyboard() -> Storyboard:
    # Each shot's sound names its index, so the fake API can tell which shot a prompt belongs to.
    shots = [Shot(purpose=f"作用{i}", selling_point="", visual=f"画面{i}", camera="[推进]",
                  sound=f"呼啸声{i}", caption=f"测试字幕【第{i}个】") for i in range(1, 4)]
    return Storyboard(appearance="一双红色高帮鞋。", music_style="轻快流行乐", shots=shots)


def shot_of(prompt: str) -> int:
    return int(re.search(r"呼啸声(\d)", prompt).group(1)) - 1


class FakeAPIs:
    """Patches h3 + qc. Shots run in parallel threads, so results are scripted per shot index:
    `qc` and `wait` map shot -> results consumed in order; `submit_error` maps shot -> exception raised once."""

    def __init__(self, qc=None, wait=None, submit_error=None):
        self.qc = {k: list(v) for k, v in (qc or {}).items()}
        self.wait_results = {k: list(v) for k, v in (wait or {}).items()}
        self.submit_error = dict(submit_error or {})
        self.submits = []  # (shot, prompt, task_id)
        self._task_shot = {}
        self._lock = threading.Lock()

    def submit(self, prompt, images, resolution):
        i = shot_of(prompt)
        with self._lock:
            if i in self.submit_error:
                raise self.submit_error.pop(i)
            tid = f"task{len(self.submits) + 1}"
            self.submits.append((i, prompt, tid))
            self._task_shot[tid] = i
        return tid

    def prompts(self, shot: int) -> list[str]:
        return [p for i, p, _ in self.submits if i == shot]

    def wait(self, task_id, timeout=None):
        with self._lock:
            results = self.wait_results.get(self._task_shot[task_id])
            return results.pop(0) if results else OK_TASK

    def download(self, url, dest):
        shutil.copy(self.clip, dest)

    def review(self, project, sb, index, video):
        with self._lock:
            results = self.qc.get(index)
            return dict(results.pop(0) if results else PASS)

    def __enter__(self):
        self._tmp = tempfile.mkdtemp()
        self.clip = Path(self._tmp) / "clip.mp4"
        make_clip(self.clip)
        self._patches = [
            mock.patch.object(h3, "submit", self.submit),
            mock.patch.object(h3, "wait", self.wait),
            mock.patch.object(h3, "download", self.download),
            mock.patch("core.qc.review", self.review),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp)


class ProjectTestCase(unittest.TestCase):
    sample = "red_hightop_sneaker"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch_dir = mock.patch.object(config, "DATA_DIR", Path(self.tmp))
        self.patch_dir.start()
        self.proj = Project.from_sample(self.sample)

    def tearDown(self):
        self.patch_dir.stop()
        shutil.rmtree(self.tmp)


class PipelineTest(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.proj.save_storyboard(fake_storyboard())
        pipeline.approve(self.proj, "480P")

    def statuses(self):
        return [s.status for s in self.proj.load_state().shots]

    def test_requires_approval(self):
        sb = self.proj.load_storyboard()
        sb.approved = False
        self.proj.save_storyboard(sb)
        with self.assertRaises(RuntimeError):
            pipeline.run(self.proj, **QUIET)

    def test_happy_path_and_assemble(self):
        with FakeAPIs() as api:
            pipeline.run(self.proj, **QUIET)
            self.assertEqual(self.statuses(), ["qc_passed"] * 3)
            self.assertEqual(len(api.submits), 3)
            self.assertIn("背景音乐：轻快流行乐", api.prompts(0)[0])  # H3 is silent without an explicit sound part
            final = assemble.assemble(self.proj, **QUIET)
        probe = ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,width,height,pix_fmt:format=duration", "-of", "compact", str(final)]
        out = subprocess.run(probe, capture_output=True, text=True).stdout
        self.assertIn("width=1080|height=1920|pix_fmt=yuv420p", out)  # yuv444p won't play on phones or Safari
        self.assertIn("codec_type=audio", out)
        duration = float(out.split("duration=")[1].split()[0])
        self.assertAlmostEqual(duration, 3 * config.SHOT_SECONDS - 2 * config.CROSSFADE, delta=0.15)

    def test_shots_run_in_parallel(self):
        barrier = threading.Barrier(3, timeout=10)  # only opens if all three shots are waiting at once

        with FakeAPIs() as api:
            def wait_together(task_id, timeout=None):
                barrier.wait()
                return OK_TASK

            with mock.patch.object(h3, "wait", wait_together):
                pipeline.run(self.proj, **QUIET)
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertEqual(len(api.submits), 3)

    def test_qc_fail_auto_retries_once_with_hint(self):
        with FakeAPIs(qc={0: [FAIL, PASS]}) as api:
            pipeline.run(self.proj, **QUIET)
        s = self.proj.load_state().shots[0]
        self.assertEqual((s.status, s.attempt, s.auto_retries), ("qc_passed", 2, 1))
        self.assertIn("特别注意：鞋面保持亮红色。", api.prompts(0)[1])
        self.assertEqual(s.video, "shots/shot1_try2.mp4")

    def test_qc_fail_twice_is_flagged_not_looped(self):
        with FakeAPIs(qc={0: [FAIL, FAIL]}) as api:
            pipeline.run(self.proj, **QUIET)
        self.assertEqual(self.statuses(), ["qc_failed", "qc_passed", "qc_passed"])
        self.assertEqual(len(api.submits), 4)  # shot1 x2, shot2, shot3

    def test_resume_repolls_instead_of_resubmitting(self):
        with FakeAPIs(wait={0: [None]}) as api:  # shot 1 poll times out (crash / closed browser)
            pipeline.run(self.proj, **QUIET)
            first = self.proj.load_state().shots[0]
            self.assertEqual(first.status, "submitted")
            pipeline.run(self.proj, **QUIET)  # resume
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertEqual(len(api.prompts(0)), 1)  # no second charge
        self.assertEqual(self.proj.load_state().shots[0].task_id, first.task_id)

    def test_failed_download_stays_submitted_and_resumes(self):
        with FakeAPIs() as api:
            real_download, failed = api.download, []

            def flaky_download(url, dest):
                if "shot1_" in dest.name and not failed:
                    failed.append(dest)
                    raise requests.ConnectionError("expired URL")
                real_download(url, dest)

            with mock.patch.object(h3, "download", flaky_download):
                pipeline.run(self.proj, **QUIET)
                self.assertEqual(self.statuses()[0], "submitted")
                pipeline.run(self.proj, **QUIET)
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertEqual(len(api.prompts(0)), 1)

    def test_failed_task_is_resubmitted_on_next_run(self):
        failed = {"status": "failed", "error": {"code": "1000", "message": "internal error"}}
        with FakeAPIs(wait={0: [failed]}):
            pipeline.run(self.proj, **QUIET)
            self.assertEqual(self.statuses(), ["failed", "qc_passed", "qc_passed"])  # one bad shot blocks nothing
            pipeline.run(self.proj, **QUIET)
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertEqual(self.proj.load_state().shots[0].attempt, 2)

    def test_moderation_needs_edit_then_retry(self):
        err = h3.H3Error("HTTP 422: video description contains sensitive content (1026)", "1026")
        with FakeAPIs(submit_error={0: err}) as api:
            pipeline.run(self.proj, **QUIET)
            s = self.proj.load_state().shots[0]
            self.assertEqual((s.status, s.moderation), ("failed", True))
            pipeline.run(self.proj, **QUIET)  # blind re-run does not resubmit
            self.assertEqual(self.proj.load_state().shots[0].status, "failed")
            pipeline.retry_shot(self.proj, 0, visual="鞋子在白色桌面上缓慢旋转", **QUIET)
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertIn("画面：鞋子在白色桌面上缓慢旋转", api.prompts(0)[-1])

    def test_manual_retry_touches_only_that_shot(self):
        with FakeAPIs() as api:
            pipeline.run(self.proj, **QUIET)
            assemble.assemble(self.proj, **QUIET)
            before = self.proj.load_state()
            pipeline.retry_shot(self.proj, 1, camera="[环绕]", sound="呼啸声2加清脆咔哒声", **QUIET)
        after = self.proj.load_state()
        self.assertEqual(after.shots[0], before.shots[0])
        self.assertEqual(after.shots[2], before.shots[2])
        self.assertEqual(after.shots[1].attempt, 2)
        self.assertEqual(after.shots[1].video, "shots/shot2_try2.mp4")
        self.assertIsNone(after.final)  # old final.mp4 is stale until re-assembled
        retried = api.prompts(1)[-1]
        self.assertIn("运镜：[环绕]", retried)
        self.assertIn("音效：呼啸声2加清脆咔哒声", retried)
        self.assertNotIn("特别注意", retried)  # a manual retry starts from the edited shot, not old QC hints

    def test_edit_caption_needs_only_reassembly(self):
        with FakeAPIs() as api:
            pipeline.run(self.proj, **QUIET)
            assemble.assemble(self.proj, **QUIET)
            pipeline.edit_caption(self.proj, 0, "新的【字幕】")
        self.assertEqual(self.proj.load_storyboard().shots[0].caption, "新的【字幕】")
        self.assertIsNone(self.proj.load_state().final)
        self.assertEqual(self.statuses(), ["qc_passed"] * 3)
        self.assertEqual(len(api.submits), 3)  # nothing regenerated

    def test_assemble_refuses_unfinished_shots(self):
        with FakeAPIs(wait={0: [None]}):
            pipeline.run(self.proj, **QUIET)
        with self.assertRaises(RuntimeError):
            assemble.assemble(self.proj, **QUIET)

    def test_assemble_handles_clip_without_audio(self):
        with FakeAPIs() as api:
            make_clip(api.clip, audio=False)
            pipeline.run(self.proj, **QUIET)
            final = assemble.assemble(self.proj, **QUIET)
        self.assertTrue(final.exists())


class StoryboardTest(ProjectTestCase):
    sample = "ceramic_mug"

    def test_failed_qc_triggers_one_revision(self):
        draft = fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS)
        revised = fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS)
        revised["shots"][0]["caption"] = "修正后的【字幕】"
        with replies(draft, {"pass": False, "issues": ["镜头1有问题"]}, revised, {"pass": True, "issues": []}) as ask:
            sb = storyboard.generate(self.proj, **QUIET)
        self.assertEqual(ask.call_count, 4)
        self.assertEqual(sb.shots[0].caption, "修正后的【字幕】")
        self.assertTrue(sb.qc["pass"])
        self.assertFalse(sb.approved)
        self.assertEqual(self.proj.load_storyboard(), sb)

    def test_long_caption_fails_qc_even_if_m3_passes_it(self):
        sb = fake_storyboard()
        sb.shots[2].caption = "这是一条明显超过字数上限的【字幕】文案内容"
        with replies({"pass": True, "issues": []}):
            result = storyboard.check(self.proj, sb)
        self.assertFalse(result["pass"])

    def test_every_save_bumps_the_revision(self):
        sb = fake_storyboard()
        self.proj.save_storyboard(sb)
        self.proj.save_storyboard(sb)
        self.assertEqual(self.proj.load_storyboard().revision, 2)

    def test_regenerated_storyboard_gets_a_new_revision(self):
        # A fresh Storyboard object starts at revision 0; saving it must not reuse the stored revision number,
        # or the UI keeps showing the old text under the same widget keys.
        draft = fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS)
        passed = {"pass": True, "issues": []}
        with replies(draft, passed, draft, passed):
            first = storyboard.generate(self.proj, **QUIET).revision
            second = storyboard.generate(self.proj, **QUIET).revision
        self.assertGreater(second, first)

    def test_m3_cannot_approve_its_own_storyboard(self):
        draft = {**fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS), "approved": True, "revision": 99}
        with replies(draft, {"pass": True, "issues": []}):
            sb = storyboard.generate(self.proj, **QUIET)
        self.assertFalse(sb.approved)
        self.assertEqual(sb.revision, 1)

    def test_selling_points_are_stored_verbatim(self):
        proj = Project.create("垃圾袋", ["- 自动收口", "1. 大容量"], self.proj.images, "upload")
        self.assertEqual(proj.product["selling_points"], ["- 自动收口", "1. 大容量"])


class VariantTest(unittest.TestCase):
    def test_prompt_names_variants_only_when_there_are_several(self):
        single = storyboard.h3_prompt(fake_storyboard(), 0)
        self.assertNotIn("款式：", single)
        sb = fake_storyboard()
        sb.variants = "黑色表盘款、白色表盘款"
        multi = storyboard.h3_prompt(sb, 0)
        self.assertIn("款式：黑色表盘款、白色表盘款", multi)
        self.assertIn("不能混合不同款式的特征", multi)

    def test_storyboards_saved_before_variants_still_load(self):
        old = fake_storyboard().model_dump(exclude={"variants"})
        self.assertEqual(Storyboard.model_validate(old).variants, "")


class CaptionAndAudioTest(unittest.TestCase):
    def test_caption_parts(self):
        self.assertEqual(storyboard.caption_parts("这鞋底会【变色】"), [("这鞋底会", False), ("变色", True)])
        self.assertEqual(storyboard.caption_parts("【冲】就完了"), [("冲", True), ("就完了", False)])

    def test_caption_issues(self):
        self.assertEqual(storyboard.caption_issues("这鞋底会【变色】"), [])
        self.assertEqual(len(storyboard.caption_issues("没有高亮词")), 1)
        self.assertEqual(len(storyboard.caption_issues("这是一条明显超过字数上限的字幕文案")), 2)

    def test_silent_clip_fails_qc_even_if_m3_passes_it(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            make_clip(tmp / "silent.mp4", audio=False)
            proj = mock.Mock(images=[], product={"name": "x"})
            with replies(PASS), mock.patch("core.m3.video_block"):
                result = qc.review(proj, fake_storyboard(), 0, tmp / "silent.mp4")
            self.assertFalse(result["pass"])
            self.assertIn("音乐和音效要清晰响亮", result["retry_hint"])
        finally:
            shutil.rmtree(tmp)


class M3ContractTest(unittest.TestCase):
    """M3 returns text: valid JSON is not the same as a valid verdict."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_clip(self.tmp / "clip.mp4")
        self.proj = mock.Mock(images=[], product={"name": "x"})
        self.video_block = mock.patch("core.m3.video_block")
        self.video_block.start()

    def tearDown(self):
        self.video_block.stop()
        shutil.rmtree(self.tmp)

    def review(self):
        return qc.review(self.proj, fake_storyboard(), 0, self.tmp / "clip.mp4")

    def test_string_values_are_read_by_meaning(self):
        with replies({"pass": "false", "product_fidelity": "5", "visual_quality": "5", "issues": [], "retry_hint": ""}):
            result = self.review()
        self.assertIs(result["pass"], False)  # bool("false") would have been True
        self.assertEqual(result["product_fidelity"], 5)

    def test_out_of_contract_reply_is_retried_once(self):
        bad = {"pass": True, "product_fidelity": 9, "visual_quality": "great"}
        with replies(bad, PASS) as ask:
            result = self.review()
        self.assertEqual(ask.call_count, 2)
        self.assertTrue(result["pass"])

    def test_still_invalid_after_retry_raises_instead_of_guessing(self):
        bad = {"pass": "maybe", "product_fidelity": 5, "visual_quality": 5}
        with replies(bad, bad), self.assertRaises(ValueError):
            self.review()


class ConcurrencyAndRecoveryTest(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.proj.save_storyboard(fake_storyboard())
        pipeline.approve(self.proj, "480P")

    def test_second_operation_on_a_busy_project_is_refused(self):
        entered, release = threading.Event(), threading.Event()

        def slow_wait(task_id, timeout=None):
            entered.set()
            release.wait(10)
            return OK_TASK

        with FakeAPIs() as api, mock.patch.object(h3, "wait", slow_wait):
            worker = threading.Thread(target=pipeline.run, args=(self.proj,), kwargs=QUIET)
            worker.start()
            self.assertTrue(entered.wait(10))
            for operation in (lambda: pipeline.run(self.proj, **QUIET),
                              lambda: pipeline.retry_shot(self.proj, 0, **QUIET),
                              lambda: pipeline.edit_caption(self.proj, 0, "别的【字幕】"),
                              lambda: assemble.assemble(self.proj, **QUIET)):
                with self.assertRaises(ProjectBusy):
                    operation()
            release.set()
            worker.join(10)
        self.assertEqual(len(api.submits), 3)  # not 6

    def test_lock_holds_across_processes(self):
        hold = ("import fcntl, sys, time; f = open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX); "
                "print('locked', flush=True); time.sleep(30)")
        other = subprocess.Popen([sys.executable, "-c", hold, str(self.proj.dir / ".lock")],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(other.stdout.readline().strip(), "locked")
            with self.assertRaises(ProjectBusy):
                pipeline.run(self.proj, **QUIET)
        finally:
            other.kill()
            other.wait()
            other.stdout.close()
        with FakeAPIs():
            pipeline.run(self.proj, **QUIET)  # free again once the other process is gone
        self.assertTrue(self.proj.load_state().all_done)

    def test_retry_drives_only_the_requested_shot(self):
        with FakeAPIs(submit_error={1: h3.H3Error("HTTP 500: internal error (1000)", "1000")}) as api:
            pipeline.run(self.proj, **QUIET)
            self.assertEqual([s.status for s in self.proj.load_state().shots], ["qc_passed", "failed", "qc_passed"])
            pipeline.retry_shot(self.proj, 0, **QUIET)
        statuses = [s.status for s in self.proj.load_state().shots]
        self.assertEqual(statuses, ["qc_passed", "failed", "qc_passed"])  # shot 2 left alone
        self.assertEqual(api.prompts(1), [])  # and never resubmitted
        self.assertEqual(len(api.prompts(0)), 2)

    def test_failed_reassembly_keeps_the_previous_final(self):
        with FakeAPIs():
            pipeline.run(self.proj, **QUIET)
            final = assemble.assemble(self.proj, **QUIET)
        before = final.read_bytes()

        def broken_join(segments, dest):
            dest.write_bytes(b"half-written")
            raise RuntimeError("ffmpeg failed")

        with mock.patch.object(assemble, "_join", broken_join), self.assertRaises(RuntimeError):
            assemble.assemble(self.proj, **QUIET)
        self.assertEqual(final.read_bytes(), before)
        self.assertEqual(self.proj.load_state().final, "final.mp4")
        self.assertFalse((self.proj.dir / "work" / "final.tmp.mp4").exists())


class DraftWritesTest(ProjectTestCase):
    """Every write from the editor goes through a locked operation that refuses outdated edits."""

    def setUp(self):
        super().setUp()
        self.proj.save_storyboard(fake_storyboard())

    def editor_copy(self) -> Storyboard:
        return self.proj.load_storyboard()  # what the page loaded: carries the revision it started from

    def test_save_on_top_of_a_newer_storyboard_is_refused(self):
        mine, theirs = self.editor_copy(), self.editor_copy()
        theirs.shots[0].caption = "他们的【字幕】"
        storyboard.save_draft(self.proj, theirs)
        mine.shots[0].caption = "我的【字幕】"
        with self.assertRaises(StaleStoryboard):
            storyboard.save_draft(self.proj, mine)
        self.assertEqual(self.proj.load_storyboard().shots[0].caption, "他们的【字幕】")

    def test_draft_save_is_refused_while_the_project_is_busy(self):
        with self.proj.lock(), self.assertRaises(ProjectBusy):
            storyboard.save_draft(self.proj, self.editor_copy())

    def test_confirm_during_a_recheck_is_not_undone(self):
        # The review's scenario: M3 is checking the draft when another session confirms it.
        edited = self.editor_copy()

        def check_while_someone_confirms(project, sb):
            pipeline.approve(self.proj, "480P")
            return {"pass": True, "issues": []}

        with mock.patch.object(storyboard, "check", check_while_someone_confirms), self.assertRaises(StaleStoryboard):
            storyboard.recheck(self.proj, edited)
        self.assertTrue(self.proj.load_storyboard().approved)
        self.assertIsNotNone(self.proj.load_state())

    def test_confirm_with_outdated_edits_is_refused(self):
        stale = self.editor_copy()
        storyboard.save_draft(self.proj, self.editor_copy())
        with self.assertRaises(StaleStoryboard):
            pipeline.approve(self.proj, "480P", edited=stale)
        self.assertFalse(self.proj.load_storyboard().approved)

    def test_confirming_twice_does_not_wipe_progress(self):
        pipeline.approve(self.proj, "480P")
        with FakeAPIs():
            pipeline.run(self.proj, **QUIET)
        with self.assertRaises(RuntimeError):
            pipeline.approve(self.proj, "480P")
        self.assertTrue(self.proj.load_state().all_done)


class ConsistencyTest(ProjectTestCase):
    """Storyboard, shot state and final must always agree, even if a write fails part-way."""

    def setUp(self):
        super().setUp()
        self.proj.save_storyboard(fake_storyboard())
        pipeline.approve(self.proj, "480P")
        with FakeAPIs():
            pipeline.run(self.proj, **QUIET)
            assemble.assemble(self.proj, **QUIET)

    def regenerate(self):
        draft = fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS)
        draft["shots"][0]["caption"] = "新分镜的【字幕】"
        with replies(draft, {"pass": True, "issues": []}):
            return storyboard.generate(self.proj, **QUIET)

    def test_regenerating_the_storyboard_invalidates_old_shots(self):
        sb = self.regenerate()
        self.assertFalse(sb.approved)
        self.assertIsNone(self.proj.load_state())
        with self.assertRaises(RuntimeError):  # no new captions on old shots
            assemble.assemble(self.proj, **QUIET)

    def test_regeneration_interrupted_midway_never_leaves_old_shots_valid(self):
        with mock.patch.object(Project, "clear_state", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.regenerate()
        self.assertFalse(self.proj.load_storyboard().approved)  # un-approved before anything else changed
        with self.assertRaises(RuntimeError):
            assemble.assemble(self.proj, **QUIET)
        with self.assertRaises(RuntimeError):
            pipeline.run(self.proj, **QUIET)

    def fail_storyboard_saves(self):
        return mock.patch.object(Project, "save_storyboard", side_effect=OSError("disk full"))

    def test_caption_edit_interrupted_midway_drops_the_final_first(self):
        with self.fail_storyboard_saves(), self.assertRaises(OSError):
            pipeline.edit_caption(self.proj, 0, "新的【字幕】")
        self.assertIsNone(self.proj.load_state().final)  # no final left claiming to match

    def test_retry_interrupted_midway_leaves_the_shot_to_regenerate(self):
        with self.fail_storyboard_saves(), self.assertRaises(OSError):
            pipeline.retry_shot(self.proj, 1, visual="新的画面", **QUIET)
        st = self.proj.load_state()
        self.assertEqual(st.shots[1].status, "pending")
        self.assertIsNone(st.final)

    def test_approval_interrupted_midway_is_not_approved(self):
        sb = self.regenerate()
        with self.fail_storyboard_saves(), self.assertRaises(OSError):
            pipeline.approve(self.proj, "480P")
        self.assertFalse(self.proj.load_storyboard().approved)
        self.assertEqual(self.proj.load_storyboard().revision, sb.revision)
        pipeline.approve(self.proj, "480P")  # confirming again works
        self.assertTrue(self.proj.load_storyboard().approved)


class UploadTest(unittest.TestCase):
    def test_normalize_images(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            Image.new("RGBA", (3000, 2000), "red").save(tmp / "big.png")
            Image.new("RGB", (200, 200)).save(tmp / "tiny.jpg")
            Image.new("RGB", (3000, 600)).save(tmp / "wide.jpg")
            out = tmp / "out"
            out.mkdir()
            [p] = normalize_images([tmp / "big.png"], out)
            with Image.open(p) as im:
                self.assertEqual((im.format, im.mode, max(im.size)), ("JPEG", "RGB", 2048))
            with self.assertRaises(ValueError):
                normalize_images([tmp / "tiny.jpg"], out)
            with self.assertRaises(ValueError):
                normalize_images([tmp / "wide.jpg"], out)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
