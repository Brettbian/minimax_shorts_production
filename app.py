"""Streamlit UI: product -> M3 storyboard -> user confirms -> H3 shots (+ M3 QC, retry, resume) -> final video.

Run locally:  .venv/bin/streamlit run app.py
"""
import contextlib
import json
import os
import tempfile
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="图文生成短视频", page_icon="🎬", layout="wide")

# Streamlit Cloud keeps settings in st.secrets; copy them into the environment before core.config reads it.
try:
    for _key in ("MINIMAX_API_KEY", "MINIMAX_API_BASE", "APP_PASSWORD", "FONT_PATH"):
        if _key in st.secrets:
            os.environ.setdefault(_key, str(st.secrets[_key]))
except FileNotFoundError:  # no secrets.toml locally; .env is used instead
    pass

from core import assemble, config, pipeline, storyboard  # noqa: E402
from core.project import Project, Shot, ShotState, State, Storyboard, normalize_images  # noqa: E402

NEW_PROJECT = "➕ 新建项目"
STATUS_LABEL = {
    "pending": "⏳ 等待生成", "submitted": "🔄 生成中", "generated": "🔍 待质检",
    "qc_passed": "✅ 质检通过", "qc_failed": "⚠️ 质检未通过", "failed": "❌ 失败",
}
RETRYABLE = ("qc_passed", "qc_failed", "failed")  # resting states the user can retry or edit from


@contextlib.contextmanager
def show_errors():
    """UI boundary: show a failure as a message instead of a traceback. (st.rerun/st.stop are BaseExceptions.)"""
    try:
        yield
    except Exception as e:
        st.error(f"出错了：{e}")
        st.stop()


def require_password():
    password = os.environ.get("APP_PASSWORD")
    if not password or st.session_state.get("authed"):
        return
    st.title("🎬 图文生成短视频")
    entered = st.text_input("访问密码", type="password")
    if entered == password:
        st.session_state.authed = True
        st.rerun()
    elif entered:
        st.error("密码不正确")
    st.stop()


def open_project(proj: Project):
    st.session_state.project_id = proj.id
    st.rerun()


# ---------------------------------------------------------------- sidebar and new-project form
def sidebar() -> Project | None:
    st.sidebar.title("🎬 图文生成短视频")
    options = [NEW_PROJECT, *Project.list_ids()]
    current = st.session_state.get("project_id")
    choice = st.sidebar.selectbox("项目", options, index=options.index(current) if current in options else 0,
                                  format_func=lambda pid: pid if pid == NEW_PROJECT else Project(pid).label)
    st.sidebar.caption(f"模型：{config.M3_MODEL} + {config.H3_MODEL}\n\n"
                       f"规格：{config.SHOT_COUNT} 个镜头 × {config.SHOT_SECONDS} 秒，{config.RATIO}")
    st.session_state.project_id = None if choice == NEW_PROJECT else choice
    return Project(choice) if st.session_state.project_id else None


def new_project_form():
    st.header("1. 商品信息")
    tab_sample, tab_upload = st.tabs(["使用示例商品", "上传自己的商品"])
    with tab_sample:
        samples = sorted(p.name for p in config.SAMPLES_DIR.iterdir() if (p / "product.json").exists())
        slug = st.selectbox("示例商品", samples)
        info = json.loads((config.SAMPLES_DIR / slug / "product.json").read_text())
        st.write(f"**{info['name']}**")
        st.write("\n".join(f"- {p}" for p in info["selling_points"]))
        for col, name in zip(st.columns(min(len(info["images"]), 4)), info["images"], strict=False):
            col.image(str(config.SAMPLES_DIR / slug / name))
        if st.button("用这个商品创建项目", type="primary"):
            open_project(Project.from_sample(slug))
    with tab_upload:
        name = st.text_input("商品名称")
        points = st.text_area("卖点（每行一条）", height=140)
        files = st.file_uploader(f"商品图片（同一件商品的不同角度，最多 {config.MAX_IMAGES} 张）",
                                 type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
        if st.button("创建项目", type="primary", key="create_upload"):
            selling_points = [p for p in points.splitlines() if p.strip()]
            if not name or not selling_points or not files:
                st.error("请填写商品名称、至少一条卖点，并上传至少一张图片")
                return
            try:
                images = normalize_images(files, Path(tempfile.mkdtemp()))
            except ValueError as e:
                st.error(str(e))
                return
            open_project(Project.create(name, selling_points, images, "upload"))


# ---------------------------------------------------------------- project page
def product_section(proj: Project):
    product = proj.product
    with st.expander(f"商品：{product['name']}", expanded=proj.load_storyboard() is None):
        for col, path in zip(st.columns(min(len(proj.images), 6)), proj.images, strict=False):
            col.image(str(path))
        st.write("\n".join(f"- {p}" for p in product["selling_points"]))


def run_pipeline(proj: Project, label: str):
    with show_errors(), st.status(label, expanded=True) as status:
        pipeline.run(proj, log=status.write)
        status.update(label="本轮生成结束", state="complete")


def board(proj: Project):
    """Storyboard and generated shots in one view: one fixed column per shot, from draft to retry."""
    st.header("2. 分镜与视频")
    sb = proj.load_storyboard()
    if sb is None:
        if st.button("让 M3 生成分镜", type="primary"):
            with show_errors(), st.status("M3 正在写分镜…", expanded=True) as status:
                storyboard.generate(proj, log=status.write)
            st.rerun()
        return

    if not sb.approved and sb.qc:
        if sb.qc.get("pass"):
            st.success("M3 分镜质检：通过")
        else:
            issues = "\n".join(f"- {i}" for i in sb.qc.get("issues") or [])
            st.warning(f"M3 分镜质检：有问题，建议修改后再确认\n\n{issues}")

    # Widget keys carry the project and storyboard revision: a regenerated or switched storyboard gets fresh widgets
    # instead of Streamlit keeping the previous text under the same key.
    key = f"{proj.id}:{sb.revision}"
    appearance = st.text_area("商品外观描述（所有款式共有的特征，每个镜头都会用到）", sb.appearance,
                              key=f"{key}:appearance", disabled=sb.approved, height=90)
    variants = st.text_input("款式（照片里有多个款式时，写各款的区别；只有一款时留空）", sb.variants,
                             key=f"{key}:variants", disabled=sb.approved)
    music = st.text_input("统一的背景音乐风格", sb.music_style, key=f"{key}:music", disabled=sb.approved)

    state = proj.load_state()
    if state is not None:
        if st.session_state.pop(f"autorun:{proj.id}", False):
            run_pipeline(proj, "H3 正在同时生成三个镜头，M3 逐个质检…")
            state = proj.load_state()
        elif not state.all_done and st.button("▶️ 生成 / 继续未完成的镜头", type="primary",
                                              help="已完成的镜头会跳过；生成中的镜头会继续查询原任务，不会重复提交"):
            run_pipeline(proj, "继续生成…")
            st.rerun()

    shots = []
    for i, col in enumerate(st.columns(len(sb.shots))):
        with col:
            shots.append(shot_column(proj, sb, state.shots[i] if state else None, i, key))
    if not sb.approved:
        edited = dict(appearance=appearance, variants=variants, music_style=music, shots=shots)
        draft_actions(proj, sb.model_copy(update=edited))


def shot_column(proj: Project, sb: Storyboard, s: ShotState | None, i: int, key: str) -> Shot:
    """One shot's column. Returns the shot as currently edited (used by the draft's save/confirm buttons)."""
    shot = sb.shots[i]
    st.subheader(f"镜头 {i + 1}")
    st.caption(f"{shot.purpose}　|　卖点：{shot.selling_point or '—'}")
    if s is not None:
        st.write(f"{STATUS_LABEL[s.status]}　·　第 {s.attempt} 次生成")
        if s.video and (proj.dir / s.video).exists():
            st.video(str(proj.dir / s.video))
        if s.qc:
            st.caption(f"M3 质检：商品还原 {s.qc.get('product_fidelity')}/5，画质 {s.qc.get('visual_quality')}/5")
            for issue in s.qc.get("issues") or []:
                st.caption(f"- {issue}")
        if s.error:
            st.error(("内容审核未通过，请修改画面后重试。\n\n" if s.moderation else "") + s.error)

    busy = s is not None and s.status not in RETRYABLE  # being generated: wait for the run to finish
    caption = st.text_input("字幕（【】内为高亮词）", shot.caption, key=f"{key}:cap{i}", disabled=busy)
    visual = st.text_area("画面", shot.visual, key=f"{key}:vis{i}", disabled=busy, height=130)
    camera = st.text_input("运镜", shot.camera, key=f"{key}:cam{i}", disabled=busy)
    sound = st.text_input("音效", shot.sound, key=f"{key}:snd{i}", disabled=busy)

    if s is not None and not busy:
        if caption != shot.caption:
            for problem in storyboard.caption_issues(caption):
                st.warning(problem)
            if st.button("💾 保存字幕", key=f"{key}:savecap{i}",
                         help="字幕只在合成时叠加，不用重新生成视频；保存后重新合成即可"):
                pipeline.edit_caption(proj, i, caption)
                st.rerun()
        edits = {k: v for k, v in {"visual": visual, "camera": camera, "sound": sound}.items() if v != getattr(shot, k)}
        if s.moderation and not edits:
            st.caption("内容审核未通过：请先修改画面再重新生成")
        if st.button("🔁 按修改重新生成此镜头" if edits else "🔁 重新生成此镜头", key=f"{key}:retry{i}",
                     type="primary" if edits else "secondary", disabled=s.moderation and not edits):
            with show_errors(), st.status(f"重新生成镜头 {i + 1}…", expanded=True) as status:
                pipeline.retry_shot(proj, i, **edits, log=status.write)
            st.rerun()
        if edits:
            st.caption("商品本身的样子以照片为准：如果修改和照片冲突（比如颜色），视频会按照片生成。")
    if s is not None and s.prompt:
        with st.expander("原提示词"):
            st.text_area("上一次发给 H3 的提示词（不可修改）", s.prompt, key=f"{key}:prompt{i}:{s.attempt}",
                         disabled=True, height=280)
    return shot.model_copy(update=dict(caption=caption, visual=visual, camera=camera, sound=sound))


def draft_actions(proj: Project, edited: Storyboard):
    """Every write goes through a locked core operation that refuses edits made on top of an outdated storyboard."""
    save, recheck, regenerate, confirm = st.columns([1, 1, 1, 2])
    if save.button("保存修改"):
        with show_errors():
            storyboard.save_draft(proj, edited)
        st.session_state.toast = "已保存"
        st.rerun()  # the revision changed: re-render now so the next edit lands in the new widgets
    if recheck.button("重新检查"):
        with show_errors(), st.spinner("M3 检查中…"):
            storyboard.recheck(proj, edited)
        st.rerun()
    if regenerate.button("重新生成分镜"):
        with show_errors(), st.status("M3 正在重写分镜…", expanded=True) as status:
            storyboard.generate(proj, log=status.write)
        st.rerun()
    with confirm:
        resolution = st.radio("视频清晰度", config.RESOLUTIONS, horizontal=True,
                              help="480P 更快更便宜，适合调试；768P 画质更好")
        if st.button("✅ 确认分镜，开始生成视频", type="primary"):
            with show_errors():
                pipeline.approve(proj, resolution, edited=edited)
            st.session_state[f"autorun:{proj.id}"] = True
            st.rerun()


def final_section(proj: Project, state: State | None):
    if state is None:
        return
    st.header("3. 成片")
    if not state.all_done:
        st.caption("所有镜头生成完成后即可合成。")
        return
    flagged = [i + 1 for i, s in enumerate(state.shots) if s.status == "qc_failed"]
    if flagged:
        st.warning(f"镜头 {flagged} 质检未通过，仍可合成；建议先重试这些镜头。")
    if st.button("🎞️ 重新合成" if state.final else "🎞️ 合成最终视频", type="primary"):
        with show_errors(), st.status("ffmpeg 合成中（统一 1080×1920、加字幕、转场）…", expanded=True) as status:
            assemble.assemble(proj, log=status.write)
            status.update(label="合成完成", state="complete")
        st.rerun()
    if state.final:
        final = proj.dir / state.final
        _, middle, _ = st.columns([1, 1, 1])
        with middle:
            st.video(str(final))
            st.download_button("⬇️ 下载 MP4", final.read_bytes(), file_name=f"{proj.id}.mp4", mime="video/mp4",
                               use_container_width=True)
            st.caption("云端的文件会定期清空，请及时下载成片。")


def main():
    require_password()
    if toast := st.session_state.pop("toast", None):
        st.toast(toast)
    if not config.API_KEY:
        st.error("缺少 MINIMAX_API_KEY：本地请写在 .env，Streamlit Cloud 请写在 Secrets 里。")
        st.stop()
    proj = sidebar()
    if proj is None:
        new_project_form()
        return
    st.title(proj.product["name"])
    st.caption(f"项目 {proj.id}")
    product_section(proj)
    board(proj)
    final_section(proj, proj.load_state())


main()
