"""Page tests with Streamlit's AppTest: the page must agree with the core about which state is valid.

  .venv/bin/python -m unittest discover tests
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from core import assemble, config, pipeline, storyboard
from core.project import Project
from tests.test_pipeline import QUIET, FakeAPIs, fake_storyboard, replies

APP = Path(__file__).resolve().parent.parent / "app.py"


class RecoveryPageTest(unittest.TestCase):
    """Writes go dependents-first, so a crash can leave state.json next to an unapproved storyboard.
    The page must then show an editable draft, not stale progress or an old final."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(config, "DATA_DIR", Path(self.tmp)),
            mock.patch.object(config, "API_KEY", "test-key"),
            mock.patch.dict(os.environ, {"APP_PASSWORD": ""}),
        ]
        for p in self.patches:
            p.start()
        self.proj = Project.from_sample("ceramic_mug")
        self.proj.save_storyboard(fake_storyboard())

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def render(self) -> AppTest:
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.session_state["project_id"] = self.proj.id
        at.run()
        self.assertFalse(at.exception, at.exception)
        return at

    def finish_project(self):
        pipeline.approve(self.proj, "480P")
        with FakeAPIs():
            pipeline.run(self.proj, **QUIET)
            assemble.assemble(self.proj, **QUIET)

    def assert_editable_draft(self, at: AppTest):
        captions = [w for w in at.text_input if w.label.startswith("字幕")]
        self.assertEqual(len(captions), 3)
        self.assertFalse(any(w.disabled for w in captions))
        labels = [b.label for b in at.button]
        self.assertIn("✅ 确认分镜，开始生成视频", labels)
        self.assertFalse(any("继续未完成" in label for label in labels))
        self.assertNotIn("3. 成片", [h.value for h in at.header])
        self.assertEqual(len(at.download_button), 0)

    def test_confirm_interrupted_after_writing_state_shows_an_editable_draft(self):
        with mock.patch.object(Project, "save_storyboard", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            pipeline.approve(self.proj, "480P")
        self.assertIsNotNone(self.proj.load_state())  # the stale state the page must ignore
        self.assert_editable_draft(self.render())

    def test_regeneration_interrupted_before_clearing_state_hides_the_old_final(self):
        self.finish_project()
        draft = fake_storyboard().model_dump(include=storyboard.CONTENT_FIELDS)
        with replies(draft, {"pass": True, "issues": []}), \
                mock.patch.object(Project, "clear_state", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            storyboard.generate(self.proj, **QUIET)
        self.assertEqual(self.proj.load_state().final, "final.mp4")  # the stale final the page must ignore
        self.assert_editable_draft(self.render())

    def test_finished_project_shows_shots_and_final(self):
        self.finish_project()
        at = self.render()
        self.assertIn("3. 成片", [h.value for h in at.header])
        self.assertEqual(len(at.download_button), 1)
        self.assertNotIn("✅ 确认分镜，开始生成视频", [b.label for b in at.button])


if __name__ == "__main__":
    unittest.main()
