"""M3 turns product images + selling points into a 3-shot storyboard, then checks it (one auto-revision)."""
import re

from pydantic import BaseModel, Field

from . import config, m3
from .project import Project, Shot, StaleStoryboard, Storyboard

MAX_CAPTION_CHARS = 12  # excluding the 【】 highlight marks
HIGHLIGHT = re.compile(r"【([^【】]+)】")
CONTENT_FIELDS = {"appearance", "variants", "music_style", "shots"}  # what M3 writes (vs. qc/approved/revision)

_SCHEMA = """{
  "appearance": "100字以内，只写所有款式共有的外观特征（形状、材质、结构、可见细节）；照片里只有一个款式时，也写上它的颜色",
  "variants": "照片里有多个款式（颜色、款型、尺寸、图案等不同）时，逐个列出每一款的区别，例如“黑色表盘款、白色表盘款”；只有一个款式时为空字符串",
  "music_style": "一句话描述三个镜头共用的背景音乐风格，节奏感强、适合带货",
  "shots": [
    {
      "purpose": "这个镜头的作用",
      "selling_point": "对应卖点的原文；钩子镜头可以为空字符串",
      "visual": "80字以内：场景、光线、商品和动作（从第一帧就开始动）；照片里有多个款式时，写明这个镜头展示哪一款，或几款一起出现",
      "camera": "运镜，用方括号指令，例如 [推进] [拉远] [左移] [右移] [环绕] [跟随] [固定]，可加简短说明",
      "sound": "和画面动作同步的具体音效",
      "caption": "字幕，不超过12个字（不含【】），用【】标出1个关键词"
    }
  ]
}"""


class _Draft(BaseModel):
    """What M3 may write; qc, approval and revision stay under our control (extra keys are ignored)."""
    appearance: str
    variants: str = ""
    music_style: str
    shots: list[Shot]


class StoryboardQC(BaseModel):
    passed: bool = Field(alias="pass")
    issues: list[str] = []


def _brief(product: dict) -> str:
    points = "\n".join(f"{i}. {p}" for i, p in enumerate(product["selling_points"], 1))
    return f"商品名：{product['name']}\n卖点（只能使用这些，不得编造新的功效、参数或承诺）：\n{points}"


def _generate_prompt(product: dict) -> str:
    return f"""你是抖音/TikTok 头部带货短视频的创意导演，最擅长让用户在第一秒停止滑动。
请根据上面的商品图片和下面的卖点，为一条 {config.SHOT_COUNT * config.SHOT_SECONDS} 秒的竖屏（9:16）带货短视频写分镜脚本，固定 {config.SHOT_COUNT} 个镜头，每个镜头 {config.SHOT_SECONDS} 秒。
画面由 AI 视频模型生成（商品图片会作为参考图传给它），字幕由程序在后期叠加。

{_brief(product)}

镜头结构（营销标准）：
- 镜头1「黄金一秒钩子」：第一帧就要有动态和视觉冲击（例如商品快速入场、特写扫过、灯光亮起、落地瞬间、热气升起），用近景或特写，不要远处静止的小商品。字幕是让人停下来的钩子：反差、提问或最强利益点。
- 镜头2「卖点演出来」：把最打动人的卖点在真实使用场景里演出来，根据品类选生活化场景（例如穿在脚上走路、清晨桌上倒咖啡、戴着耳机通勤的侧面或背影），不要只是把商品摆在白底上。
- 镜头3「质感英雄镜头 + 行动号召」：干净高级的布景（柔和渐变、氛围光、自然光影、简洁台面），不要出现家电、杂物或功能演示，功能演示放在镜头2；商品完整、清晰、居中，占画面高度一半以上，配一个优雅的运镜（缓慢环绕或推进）；字幕是行动号召。
- 三个镜头的场景、景别和运镜要有明显变化（例如：特写钩子 → 使用场景中景 → 完整英雄镜头），不要照搬参考图的背景。
- 每个镜头从第一帧起就要有动作，节奏明快。

商品可能有多个款式（必须遵守）：
- 先仔细比较所有照片：它们可能是同一款商品的不同角度，也可能是不同款式（颜色、款型、尺寸、图案不同）
- 不要把某一张照片里的样子当成商品唯一的样子写死：所有款式共有的特征写进 appearance，各款之间的区别写进 variants
- 有多个款式时，每个镜头的画面都要写明展示哪一款，或者几款一起出现；同一件商品身上不能混合不同款式的特征
- 字幕不要写死某一款，除非卖点本身就是针对这一款的

AI 视频模型的局限（必须遵守）：
- 每个镜头只有一个简单、物理上合理的动作，不要剧情，不要让商品自己变形、折叠或悬空翻滚
- 不要让画面里出现任何文字，不要给 logo 或包装文字做特写；背景避开招牌、指示牌、屏幕、书本这类带文字的物体
- 人物只出现手、脚、身体局部、侧面或背影，不要正脸；手部动作要简单（拿起、放下、轻触）
- 画面里的商品必须和参考图里的某一款完全一致，不能改变颜色、形状、结构或数量
- 只展示参考图里出现过的形态和角度（例如参考图只有展开状态，就不要展示折叠状态）；避免插拔、对准、拆装这类精细动作
- 不要给商品加上参考图里没有的配件或部件（例如线缆、包装盒、贴纸、额外的按钮），道具只能放在商品旁边

字幕要求（后期叠加的大字幕，手机上一眼读完）：
- 每条不超过 {MAX_CAPTION_CHARS} 个字（不含【】），是一句自然、完整、顺口的话，像真人博主会说的；不要把两个意思用逗号硬拼在一起
- 每条用【】标出 1 个最关键的词，它会被渲染成高亮色，例如“这鞋底会【变色】”“每一只都【独一无二】”
- 镜头3 的字幕是明确的行动号召，例如“喜欢就【带走它】”“这双直接【冲】”“通勤党【闭眼入】”
- 内容必须来自卖点，不得编造功效、参数、价格或承诺

所有字段都用中文填写。只输出下面格式的 JSON，不要任何其他文字：
{_SCHEMA}"""


def _check_prompt(product: dict, sb: Storyboard) -> str:
    return f"""请检查下面这个带货短视频分镜脚本是否合格。上面的图片是商品的真实照片。

{_brief(product)}

分镜脚本：
{sb.model_dump_json(include=CONTENT_FIELDS, indent=2)}

检查项：
1. 镜头1 是动态的近景钩子，不是远处静止的商品
2. 三个镜头的场景、景别或运镜有明显变化，至少一个镜头展示真实使用场景
3. 字幕没有编造卖点里没有的功效、参数或承诺，最后一个镜头有行动号召
4. visual 没有要求画面出现文字或 logo 特写，没有人物正脸，没有复杂手部操作，没有让商品变形或做物理上不可能的动作
5. 每个镜头只有一个简单动作，AI 视频模型能执行
6. appearance 和商品照片一致，只写了所有款式共有的特征，没有把某一款的颜色、款型写死
7. 照片里有多个款式时：variants 列出了各款的区别，每个镜头写明了展示哪一款，没有把不同款式的特征混在同一件商品上

只有真正违反检查项时才判为不通过，措辞风格之类的小问题不算。
issues 里只列出真正违反检查项的问题；“可以接受”“建议优化”之类的内容不要写进去。字幕长度和【】格式由程序检查。
只输出 JSON：{{"pass": true/false, "issues": ["具体问题，指明是第几个镜头"]}}"""


def caption_parts(caption: str) -> list[tuple[str, bool]]:
    """Split a caption into (text, highlighted) runs: "鞋底会【变色】" -> [("鞋底会", False), ("变色", True)]."""
    parts, pos = [], 0
    for m in HIGHLIGHT.finditer(caption):
        if m.start() > pos:
            parts.append((caption[pos:m.start()], False))
        parts.append((m.group(1), True))
        pos = m.end()
    if pos < len(caption):
        parts.append((caption[pos:], False))
    return parts


def caption_issues(caption: str) -> list[str]:
    """Rules the renderer relies on: short enough for one line, exactly one 【】 keyword."""
    issues = []
    if len("".join(t for t, _ in caption_parts(caption))) > MAX_CAPTION_CHARS:
        issues.append(f"字幕超过{MAX_CAPTION_CHARS}字")
    if len(HIGHLIGHT.findall(caption)) != 1:
        issues.append("字幕需要恰好一个【】高亮词")
    return issues


def _storyboard_caption_issues(sb: Storyboard) -> list[str]:
    return [f"镜头{i}{issue}：{s.caption}" for i, s in enumerate(sb.shots, 1) for issue in caption_issues(s.caption)]


def _ask_storyboard(product: dict, images, extra: str = "") -> Storyboard:
    content = [m3.image_block(p) for p in images]
    content.append({"type": "text", "text": _generate_prompt(product) + extra})
    sb = Storyboard(**m3.ask_json(content, _Draft).model_dump())
    if len(sb.shots) != config.SHOT_COUNT:
        raise ValueError(f"M3 返回了 {len(sb.shots)} 个镜头，应为 {config.SHOT_COUNT} 个")
    return sb


def check(project: Project, sb: Storyboard) -> dict:
    content = [m3.image_block(p) for p in project.images]
    content.append({"type": "text", "text": _check_prompt(project.product, sb)})
    result = m3.ask_json(content, StoryboardQC).model_dump(by_alias=True)
    # Caption rules are cheap to verify exactly, so don't rely on M3 counting characters.
    if issues := _storyboard_caption_issues(sb):
        result["pass"] = False
        result["issues"] = [*result["issues"], *issues]
    return result


def generate(project: Project, log=print) -> Storyboard:
    """Draft -> QC -> (if it fails) one revision -> QC again. Saves and returns the storyboard, unapproved.

    A new storyboard makes earlier shots meaningless, so the old generation is invalidated. Order matters for
    crash safety: un-approve the old storyboard first, then drop the state, then publish the new draft. A crash
    at any point leaves an unapproved storyboard, which nothing can generate from or assemble."""
    with project.lock():
        sb = _draft(project, log)
        stored = project.load_storyboard()
        if stored is not None and stored.approved:
            stored.approved = False
            project.save_storyboard(stored)
        project.clear_state()
        project.save_storyboard(sb)
        return sb


def assert_current(project: Project, edited: Storyboard):
    """`edited.revision` is the revision the editor started from; refuse if the stored one moved on or was
    confirmed in the meantime. Call with the project lock held."""
    stored = project.load_storyboard()
    if stored is None or stored.revision != edited.revision:
        raise StaleStoryboard("分镜已被其他操作更新，请刷新页面后再修改")
    if stored.approved:
        raise StaleStoryboard("分镜已经确认，不能再修改草稿")


def save_draft(project: Project, edited: Storyboard) -> Storyboard:
    """The only way to save user edits to an unconfirmed storyboard."""
    with project.lock():
        assert_current(project, edited)
        project.save_storyboard(edited)
        return edited


def recheck(project: Project, edited: Storyboard) -> Storyboard:
    """Run M3's storyboard QC on the edits and save them with the result. The M3 call runs outside the lock
    (it takes a while); the save re-checks that nobody changed the storyboard meanwhile."""
    result = check(project, edited)
    return save_draft(project, edited.model_copy(update={"qc": result}))


def _draft(project: Project, log) -> Storyboard:
    product = project.product
    log("M3 正在写分镜…")
    sb = _ask_storyboard(product, project.images)
    log("M3 正在检查分镜…")
    qc = check(project, sb)
    if not qc["pass"]:
        log(f"分镜质检未通过，自动修订一次：{'；'.join(qc['issues'])}")
        previous = sb.model_dump_json(include=CONTENT_FIELDS, indent=2)
        issues = "\n".join(qc["issues"])
        extra = f"\n\n上一版分镜：\n{previous}\n\n质检发现的问题：\n{issues}\n请修正这些问题，输出完整的新 JSON。"
        sb = _ask_storyboard(product, project.images, extra)
        qc = check(project, sb)
    sb.qc = qc
    return sb


def h3_prompt(sb: Storyboard, index: int, hint: str | None = None) -> str:
    """One field per line, so the user's text is used verbatim without being spliced into sentences."""
    s = sb.shots[index]
    lines = [
        f"{config.SHOT_SECONDS} 秒竖屏（9:16）高端商业带货视频镜头，广告级画面，对焦清晰，从第一帧开始就有流畅的运动。",
        f"商品：{sb.appearance}",
        "商品必须和参考图里的样子完全一致：形状、颜色、材质、细节和数量都不变，光线不能改变商品本身的颜色。",
    ]
    if sb.variants:
        lines += [f"款式：{sb.variants}", "参考图里有多个款式：按画面里写明的款式来画，同一件商品身上不能混合不同款式的特征。"]
    lines += [
        f"画面：{s.visual}",
        f"运镜：{s.camera}",
        "画面中不要出现任何文字、字幕、水印或额外的 logo。",
    ]
    if hint:
        lines.append(f"特别注意：{hint}")
    # H3 returns a near-silent track unless the prompt states the sound explicitly, so always end with it.
    lines += [f"背景音乐：{sb.music_style}", f"音效：{s.sound}"]
    return "\n".join(lines)
