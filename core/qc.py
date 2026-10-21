"""Video QC: M3 watches a generated clip next to the real product photos and decides pass / fail.

M3 cannot hear audio (it reports a clear test tone as silence), so the audio check is a loudness
measurement instead: H3 returns a near-silent track when the prompt's "Sound:" sentence doesn't land.
"""
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from . import config, m3
from .project import Project, Storyboard

SILENT_DB = -45.0  # mean volume below this means the clip has effectively no sound
MIN_SCORE = 4      # both product_fidelity and visual_quality must reach this


class VideoQC(BaseModel):
    """The contract for M3's verdict; lax mode turns "false" into False and "5" into 5, and rejects nonsense."""
    passed: bool = Field(alias="pass")
    product_fidelity: int = Field(ge=1, le=5)
    visual_quality: int = Field(ge=1, le=5)
    issues: list[str] = []
    retry_hint: str = ""


def mean_volume(video: Path) -> float | None:
    """Mean loudness in dB, or None if the clip has no audio stream."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    match = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(match.group(1)) if match else None


def _prompt(product: dict, sb: Storyboard, index: int) -> str:
    s = sb.shots[index]
    return f"""前面的图片是商品「{product['name']}」的真实照片，最后是 AI 生成的带货视频镜头（第 {index + 1} 个，共 {config.SHOT_COUNT} 个）。
商品外观（所有款式共有的特征）：{sb.appearance}
款式：{sb.variants or "照片里只有一个款式"}

这个镜头的设计：
- 画面：{s.visual}
- 运镜：{s.camera}

请逐段仔细看视频，尤其是运动过程中的画面。运镜造成的裁切、局部特写、景深虚化、只露出部分商品，都是有意的设计，不算问题。
商品本身的样子一律以照片为准：如果镜头设计里写的商品颜色、形状等和照片不一致，视频按照片画是对的，不算问题。
照片里可能有多个款式（颜色、款型、尺寸、图案不同）：视频里的每件商品只要和照片里的某一款一致就是对的，不要求和某一张照片一模一样。
出现下面任何一种明显问题，就判为不通过：
- 商品和照片里的任何一款都对不上：颜色、形状、结构、数量变了，或者部件多了、少了、重复了
- 同一件商品身上混合了不同款式的特征（例如一款的颜色配上另一款的形状）
- 商品上出现照片里没有的 logo、标志、文字或图案
- 运动中商品变形、扭曲、融化、闪烁或穿模
- 手、脚、身体明显畸形（手指数量不对、肢体扭曲）
- 画面里出现乱码文字，或者场景、动作、运镜和设计明显不符

打分标准：
- product_fidelity：5 = 和照片里的某一款看不出差别；4 = 只有仔细看才发现的细微差别；3 = 一眼能看出的差别（例如某个部位颜色变了、形状不同、多了标志）；2 及以下 = 明显是另一个商品
- visual_quality：5 = 广告级画面；4 = 有不影响观感的小瑕疵；3 = 有明显瑕疵（变形、闪烁、畸形、乱码）；2 及以下 = 不可用

只输出 JSON：
{{"pass": true/false, "product_fidelity": 1-5 的整数, "visual_quality": 1-5 的整数, "issues": ["具体问题，说明大约在第几秒"], "retry_hint": "不通过时写一句要加进视频提示词的修正要求，通过时为空字符串"}}"""


def review(project: Project, sb: Storyboard, index: int, video: Path) -> dict:
    content = [m3.image_block(p) for p in project.images]
    content.append(m3.video_block(video))
    content.append({"type": "text", "text": _prompt(project.product, sb, index)})
    verdict = m3.ask_json(content, VideoQC)
    result = verdict.model_dump(by_alias=True)
    # Enforce the score bar in code so a lenient "pass" with a 3 doesn't slip through.
    result["pass"] = (verdict.passed and verdict.product_fidelity >= MIN_SCORE
                      and verdict.visual_quality >= MIN_SCORE)
    volume = mean_volume(video)
    result["mean_volume_db"] = volume
    if volume is None or volume < SILENT_DB:
        result["pass"] = False
        result["issues"] = [*result["issues"], "音轨几乎是静音"]
        result["retry_hint"] += "音乐和音效要清晰响亮。"
    return result
