"""Turn the shot clips into one 1080x1920 MP4 with burned-in Chinese captions.

Homebrew's ffmpeg has no drawtext/subtitles filter, so captions are rendered to transparent PNGs
with Pillow and laid over the video with ffmpeg's overlay filter.
"""
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config
from .project import DONE, Project
from .storyboard import caption_parts

W, H = config.OUT_W, config.OUT_H
# yuv420p/High is what phones, Safari and TikTok can play; xfade otherwise outputs yuv444p, which they reject.
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
          "-c:a", "aac", "-b:a", "160k"]
CAPTION_Y = 0.68          # caption centre as a fraction of height: above TikTok's bottom UI
CAPTION_MAX_SIZE = 108
HIGHLIGHT_COLOR = "#FFD43B"
STROKE = 9
SHADOW = (0, 0, 0, 170)


def render_caption(caption: str, dest: Path):
    """Bold white caption with a thick outline and soft shadow; the 【keyword】 is drawn in yellow."""
    parts = caption_parts(caption)
    path, index = config.caption_font()
    size = CAPTION_MAX_SIZE
    while True:  # shrink until the line fits inside the side margins
        font = ImageFont.truetype(path, size, index=index)
        widths = [font.getlength(text) for text, _ in parts]
        if sum(widths) + 2 * STROKE <= W - 140 or size <= 56:
            break
        size -= 4
    x, y = (W - sum(widths)) / 2, int(H * CAPTION_Y)

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd, td = ImageDraw.Draw(shadow), ImageDraw.Draw(text_layer)
    for (text, highlighted), width in zip(parts, widths, strict=True):
        sd.text((x, y + 8), text, font=font, anchor="lm", fill=SHADOW, stroke_width=STROKE, stroke_fill=SHADOW)
        td.text((x, y), text, font=font, anchor="lm", fill=HIGHLIGHT_COLOR if highlighted else "white",
                stroke_width=STROKE, stroke_fill="black")
        x += width
    Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)), text_layer).save(dest)


def _has_audio(video: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    return any(s["codec_type"] == "audio" for s in json.loads(out)["streams"])


def _ffmpeg(args: list):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")


def _segment(video: Path, caption_png: Path, dest: Path):
    """Scale/crop to 1080x1920, fix fps, trim to exactly SHOT_SECONDS, fade the caption in, normalise audio."""
    seconds = str(config.SHOT_SECONDS)
    inputs = ["-i", str(video), "-loop", "1", "-t", seconds, "-i", str(caption_png)]
    audio_in = "0:a"
    if not _has_audio(video):
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        audio_in = "2:a"
    graph = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={config.OUT_FPS},setsar=1[base];"
        f"[1:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1[cap];"
        f"[base][cap]overlay=0:0,format=yuv420p[v];"
        f"[{audio_in}]aresample=44100,aformat=channel_layouts=stereo[a]"
    )
    _ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", seconds, *ENCODE, str(dest)])


def _join(segments: list[Path], dest: Path):
    """Crossfade video (xfade) and audio (acrossfade) between consecutive segments."""
    if len(segments) == 1:
        shutil.copy(segments[0], dest)
        return
    d, length = config.CROSSFADE, config.SHOT_SECONDS
    inputs = [arg for seg in segments for arg in ("-i", str(seg))]
    parts, v, a = [], "[0:v]", "[0:a]"
    for k in range(1, len(segments)):
        parts.append(f"{v}[{k}:v]xfade=transition=fade:duration={d}:offset={k * (length - d):.3f}[v{k}]")
        parts.append(f"{a}[{k}:a]acrossfade=d={d}[a{k}]")
        v, a = f"[v{k}]", f"[a{k}]"
    _ffmpeg([*inputs, "-filter_complex", ";".join(parts), "-map", v, "-map", a, *ENCODE,
             "-movflags", "+faststart", str(dest)])


def assemble(project: Project, log=print) -> Path:
    with project.lock():
        return _assemble(project, log)


def _assemble(project: Project, log) -> Path:
    sb, st = project.load_storyboard(), project.load_state()
    if sb is None or not sb.approved or st is None or len(st.shots) != len(sb.shots):
        raise RuntimeError("还没有和当前分镜对应的镜头：请先确认分镜并生成")
    missing = [i + 1 for i, s in enumerate(st.shots) if s.status not in DONE]
    if missing:
        raise RuntimeError(f"镜头 {missing} 还没有生成完成")
    flagged = [i + 1 for i, s in enumerate(st.shots) if s.status == "qc_failed"]
    if flagged:
        log(f"注意：镜头 {flagged} 质检未通过，仍然合成（重试这些镜头效果会更好）")

    work = project.dir / "work"
    work.mkdir(exist_ok=True)
    segments = []
    for i, (shot, s) in enumerate(zip(sb.shots, st.shots, strict=True)):
        png, seg = work / f"caption{i + 1}.png", work / f"seg{i + 1}.mp4"
        render_caption(shot.caption, png)
        _segment(project.dir / s.video, png, seg)
        segments.append(seg)
        log(f"镜头 {i + 1} 已处理（统一尺寸、加字幕）")
    # Encode beside the published file and swap it in only on success, so a failed run keeps the previous final.
    final, tmp = project.dir / "final.mp4", work / "final.tmp.mp4"
    try:
        _join(segments, tmp)
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)
    st.final = final.name
    project.save_state(st)
    log(f"成片已生成：{final.name}")
    return final
