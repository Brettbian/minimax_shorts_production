"""MiniMax-M3 through its Anthropic-compatible endpoint. M3 reads images and video (but not audio)."""
import base64
import functools
import json
import mimetypes
from pathlib import Path

import anthropic
from pydantic import BaseModel

from . import config


@functools.cache
def client() -> anthropic.Anthropic:
    if not config.API_KEY:
        raise RuntimeError("MINIMAX_API_KEY is not set (see .env.example)")
    return anthropic.Anthropic(base_url=f"{config.API_BASE}/anthropic", api_key=config.API_KEY)


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def image_block(path: Path) -> dict:
    media = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": _b64(path)}}


def video_block(path: Path) -> dict:
    # Not an Anthropic block type, but MiniMax accepts it on the same endpoint (mp4 <= 50 MB).
    return {"type": "video", "source": {"type": "base64", "media_type": "video/mp4", "data": _b64(path)}}


def ask(content: list, max_tokens: int = 16000) -> str:
    """One user turn; returns the text blocks joined. Thinking tokens count toward max_tokens."""
    resp = client().messages.create(
        model=config.M3_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        extra_body={"output_config": {"effort": config.M3_EFFORT}},
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in M3 reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def ask_json[M: BaseModel](content: list, schema: type[M]) -> M:
    """M3 has no structured-output mode: parse the reply and validate it against `schema`, asking once more
    (with the reason) if it isn't valid JSON or doesn't fit. Pydantic's ValidationError is a ValueError."""
    try:
        return schema.model_validate(_extract_json(ask(content)))
    except ValueError as e:
        text = f"上一次的回复不符合要求：{str(e)[:300]}\n只输出符合格式的 JSON 对象本身，不要任何其他文字。"
        nudge = {"type": "text", "text": text}
        return schema.model_validate(_extract_json(ask([*content, nudge])))
