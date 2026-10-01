"""第 3.5 段：重点句。把"哪几句是重点"翻译成"哪一段时间"，并切出音频段。

为什么单独一段
--------------
- **判断哪句是重点**是语义活（优惠、福利、促单）→ 第 1 段由模型写 `keyphrases.json`
- **那句话说在什么时间**是确定性的 → 从 `align/<shot-id>.json` 的逐字时间戳里查

这两件事必须分开：模型给的只是文字，时间只能来自对齐结果，不能靠猜或均分。

产物
----
1. `keyphrases_resolved.json` —— 带明确起止时间的重点句清单，第 4 段消费
2. `audio/<shot-id>_s<k>.wav` —— 重点句对应的音频段

为什么要切音频段：`effects`（场景音/音色/声音成曲）只能挂在**音频段**上。
重点句通常在某句话中间，所以必须先把那一段切出来单独挂效果，
否则只能给整条音轨挂，那就不是"重点句加音效"了。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from . import align as align_mod
from . import artifacts


class KeyphraseError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


def _norm(text: str) -> str:
    """和 align 用同一套归一化（去标点空白 + 转简体），否则下标对不上。"""
    return align_mod.normalize(text)


def resolve_phrase(align: dict, phrase: str) -> tuple[float, float, int, int]:
    """在某个镜头的对齐结果里查出这句话的起止时间（镜头内相对秒）。

    同时返回它在**归一化台词文本**里的下标和长度 —— 第 4 段要靠这两个值
    把字幕按字符切开，光有时间切不准。
    """
    chars = align.get("chars") or []
    if not chars:
        raise KeyphraseError("该镜头没有逐字时间戳，先跑 vcp align")

    expected = _norm(align.get("expected_text") or "")
    needle = _norm(phrase)
    if not needle:
        raise KeyphraseError("重点句的 text 去标点后是空的")

    idx = expected.find(needle)
    if idx < 0:
        raise KeyphraseError(
            f"「{phrase}」不是该镜头台词的一部分。\n"
            f"  镜头台词：{align.get('expected_text')}\n"
            "重点句必须逐字出现在台词里（标点可以不同），否则定位不到时间。"
        )
    span = chars[idx:idx + len(needle)]
    if not span:
        raise KeyphraseError(f"「{phrase}」定位到了下标 {idx} 但取不到时间戳")
    return float(span[0]["start"]), float(span[-1]["end"]), idx, len(needle)


def load_declared(project_dir: Path) -> list[dict]:
    path = Path(project_dir) / "keyphrases.json"
    if not path.is_file():
        return []
    data = artifacts.read_json(path)
    entries = data.get("keyphrases") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise KeyphraseError("keyphrases.json 里 keyphrases 必须是数组")
    return entries


def resolve(cfg, project_dir: Path) -> dict:
    project_dir = Path(project_dir)
    media = _media(cfg)
    declared = load_declared(project_dir)

    if not declared:
        # 没有就写个空清单，让第 4 段走"全部字幕都不带动画"的正常分支
        out = {"keyphrases": [], "note": "项目里没有 keyphrases.json，按无重点句处理"}
        artifacts.write_json(project_dir / "keyphrases_resolved.json", out)
        return out

    aligns = artifacts.load_align_all(project_dir)
    pad = float(cfg.get("keyphrase.sfx_pad", 0.2))
    default_anim = cfg.get("keyphrase.default_animation") or {}
    default_sfx = cfg.get("keyphrase.default_sfx") or {}
    cuts = bool(cfg.get("keyphrase.cuts_audio", True))

    per_shot: dict[str, int] = {}
    resolved: list[dict] = []

    for item in declared:
        sid = item.get("shot_id")
        text = (item.get("text") or "").strip()
        if not sid or not text:
            raise KeyphraseError(f"重点句条目缺 shot_id 或 text：{item}")

        align = aligns.get(sid)
        if align is None:
            raise KeyphraseError(f"{sid}: 没有 align/{sid}.json，先跑 vcp align")
        if align.get("skipped"):
            raise KeyphraseError(
                f"{sid}: 该镜头没有台词（align 跳过了），不可能有重点句"
            )

        start, end, norm_idx, norm_len = resolve_phrase(align, text)
        shot = next((s for s in artifacts.load_shots(project_dir)["shots"]
                     if s["id"] == sid), None)
        if shot is None:
            raise KeyphraseError(f"{sid}: shots.json 里没有这个镜头")

        hmm = min(pad, max(0.0, start))          # 往前留一点，别越界
        tail = min(pad, max(0.0, float(shot["duration"]) - end))
        seg_start, seg_end = start - hmm, end + tail

        n = per_shot.get(sid, 0) + 1
        per_shot[sid] = n
        rel = f"audio/{sid}_s{n}.wav"

        if cuts:
            clip = artifacts.resolved_clip(project_dir, shot, cfg)
            if not clip.is_file():
                raise KeyphraseError(f"{sid}: 找不到片段 {clip}，无法切音轨")
            media.cut_audio(clip, project_dir / rel, seg_start, seg_end)

        resolved.append({
            "shot_id": sid,
            "text": text,
            "note": item.get("note"),
            "start": round(start, 3),
            "end": round(end, 3),
            # 归一化台词文本里的下标和长度 —— 第 4 段按它切字幕，光靠时间切不准
            "norm_start": norm_idx,
            "norm_len": norm_len,
            "audio_segment": rel if cuts else None,
            "audio_start": round(seg_start, 3),
            "audio_end": round(seg_end, 3),
            "animation": item.get("animation") or default_anim or None,
            "sfx": item.get("sfx") or default_sfx or None,
            # standalone=true 时字幕会把重点句单独拆成一段（能单独带动画）；
            # false 时动画挂在整个句子上。
            "standalone": bool(item.get("standalone", True)),
            "style": item.get("style"),
        })

    out = {"keyphrases": resolved}
    artifacts.write_json(project_dir / "keyphrases_resolved.json", out)
    return out


def _media(cfg):
    from .media import Media
    return Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))


def summarize(resolved: dict) -> list[str]:
    lines = []
    for k in resolved.get("keyphrases") or []:
        a = k.get("animation") or {}
        s = k.get("sfx") or {}
        lines.append(
            f"  {k['shot_id']}: 「{k['text']}」 {k['start']}–{k['end']}s"
            f"  动画={a.get('category')}/{a.get('name')}"
            f"  音效={s.get('category')}/{s.get('name')}"
            f"  音频段={k.get('audio_segment')}"
        )
    return lines
