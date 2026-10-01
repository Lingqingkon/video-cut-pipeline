"""第 3 段：抽音轨 → ASR 逐词时间戳 → 与台词比对（听检）→ align.json

为什么不是直接做强制对齐：
  强制对齐的算法前提是"音频念的就是你给的文本"。AIGC 生成的语音可能念错、念漏、
  重念。一旦不一致，强制对齐不会报错——它会把时间戳硬摊到你给的文本上，
  结果是错得看不出来。所以先 ASR 识别"实际说了什么"，再跟给定文本比对。

标点判断一律走 unicodedata 和码点，源码里不出现任何标点字面量——
之前把它们写进正则字面量里，引号会把字符串截断，踩过一次。

输出 align.json 的字段见 ../references/artifacts.md。
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import unicodedata
from pathlib import Path

from . import artifacts
from .media import Media

# 断句用的标点判断走 unicodedata 分类，源码里不出现标点字面量（原因见模块开头）。
# 开引号 / 开括号不能当切点，见 _breaks_sentence。
_OPENING_CATS = ("Ps", "Pi")


def _make_simplifier():
    """繁体转简体。用 zhconv（纯 Python，很小）；没装就退回不转并给一次提示。"""
    try:
        from zhconv import convert
    except ImportError:
        print("[vcp] 提示：没装 zhconv，繁简不会统一，中文比对会偏严。"
              "装一下：pip install zhconv")
        return None
    return lambda s: convert(s, "zh-cn")


_to_simplified = _make_simplifier()


def _is_punct(ch: str) -> bool:
    """Unicode 标点类：Pc Pd Pe Pf Pi Po Ps。中英文标点全都覆盖。"""
    return unicodedata.category(ch).startswith("P")


def normalize(text: str | None) -> str:
    """去标点空白 + 转简体，只留实义字符。比对和取时间戳都在这个形态上做。

    必须转简体：Whisper 对中文经常输出繁体（這裡 / 說 / 確），
    不转的话同一句话会被当成完全不同的字符，相似度直接掉到 0。
    """
    stripped = "".join(ch for ch in (text or "") if not _is_punct(ch) and not ch.isspace())
    if stripped and _to_simplified is not None:
        stripped = _to_simplified(stripped)
    return stripped


def _breaks_sentence(ch: str) -> bool:
    """这个字符能不能当切点：除开引号、开括号之外的所有 Unicode 标点。

    开引号/开括号（Ps、Pi）不能切：「『（《【 后面紧跟的是实义字，切了会先产出一段
    只有引号的空段（normalize 之后是空的、会被丢掉），引号也就跟着从字幕里消失。
    右引号、右括号要切 —— 它们本来就该挂在前一句的尾巴上。
    """
    cat = unicodedata.category(ch)
    if not cat.startswith("P"):
        return False
    return cat not in _OPENING_CATS


def split_sentences(text: str) -> list[str]:
    """按标点断句：**只要出现标点就切开**，逗号、顿号、分号也切。每段保留原标点。

    为什么从"只按句末标点"改成"按所有标点"：画布只有 480x864，一行放不下长句。
    「我想做期货交易，但看了好几家期货商，有的手续费贵，有的没专人服务」
    这种一整句压成一行，字号只能缩到看不清；按标点切开之后每段就是"一口气说的话"，
    字幕跟着停顿换行。

    时间上不会错位：调用方（align._sentences / manifest）是按 `len(normalize(段))`
    把各段依次拼回归一化全文的，而 normalize 本来就把标点算成零长度。
    所以切在哪都不影响下标连续性，只影响"怎么分组"。
    """
    src = text or ""
    out: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(src):
        ch = src[i]
        buf.append(ch)
        if _breaks_sentence(ch):
            # 连着的一串标点算同一个切点：「。」「？！」都留在同一段尾巴上。
            # 不吸收的话右引号会被切成单独一段，normalize 之后是空的，右引号就丢了。
            j = i + 1
            while j < len(src) and _breaks_sentence(src[j]):
                buf.append(src[j])
                j += 1
            segment = "".join(buf).strip()
            if normalize(segment):
                out.append(segment)
            buf = []
            i = j
            continue
        i += 1
    tail = "".join(buf).strip()
    if normalize(tail):
        out.append(tail)
    return out


def _prepare_cuda_dlls() -> list[str]:
    """把 pip 装的 nvidia-* wheel 里的 bin 目录加进 DLL 搜索路径。

    Windows 上 CTranslate2 要 cublas64_12.dll 和 cudnn_*64_9.dll。系统装了完整
    CUDA toolkit 时本来就能找到；没装的时候用 nvidia-cublas-cu12 / nvidia-cudnn-cu12
    这两个 wheel 提供，但它们的 bin 目录不在默认搜索路径里，必须手动加。
    这样做的好处是不依赖系统 CUDA，换台机器 pip 装一遍就行。
    """
    if os.name != "nt":
        return []
    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return []
    root = Path(list(spec.submodule_search_locations)[0])
    added: list[str] = []
    for bin_dir in sorted(root.glob("*/bin")):
        try:
            os.add_dll_directory(str(bin_dir))
        except (OSError, AttributeError):
            continue
        added.append(str(bin_dir))
    if added:
        # 有些依赖仍然只认 PATH
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
    return added


def load_model(cfg):
    """延迟导入——框架的其它子命令不该被 ASR 依赖绑架。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "没装 faster-whisper。在流水线 venv 里执行：\n"
            "  pip install -r requirements.txt\n"
            f"原始错误：{exc}"
        ) from exc

    model_dir = Path(cfg.require("asr.model_dir"))
    if not model_dir.is_dir():
        raise RuntimeError(
            f"asr.model_dir 指向的目录不存在：{model_dir}\n"
            "模型必须是 CTranslate2 格式（model.bin + config.json + tokenizer.json）。"
        )
    missing = [p for p in ("model.bin", "config.json", "tokenizer.json")
               if not (model_dir / p).is_file()]
    if missing:
        raise RuntimeError(f"{model_dir} 不像 CTranslate2 模型目录，缺：{', '.join(missing)}")

    device = str(cfg.get("asr.device", "cpu")).lower()
    compute_type = cfg.get("asr.compute_type")

    if device == "cuda":
        dll_dirs = _prepare_cuda_dlls()
        if not dll_dirs:
            print("[vcp] 提示：没找到 nvidia-* wheel 的 DLL 目录。"
                  "若报缺 cublas/cudnn，装 nvidia-cublas-cu12 和 nvidia-cudnn-cu12。")
        compute_type = compute_type or "float16"
    else:
        compute_type = compute_type or "int8"

    try:
        return WhisperModel(str(model_dir), device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda" and cfg.get("asr.allow_cpu_fallback", True):
            print(f"[vcp] GPU 加载失败（{type(exc).__name__}: {exc}）")
            print("[vcp] 退回 CPU。这不是理想状态，但至少能跑完——上面的原因要去修。")
            return WhisperModel(str(model_dir), device="cpu", compute_type="int8")
        raise


def _chars_from_words(words: list[dict]) -> list[dict]:
    """把 ASR 的词扩成逐字。

    中文里一个 word 常常是多个字，时间按字数均匀摊开——这是近似，
    但足够定位句子边界，也正是"字幕不再均分"所需要的最小信息。
    """
    out: list[dict] = []
    for w in words:
        # 注意键名是 "w" —— 之前这里写成 "word"，导致每个词都被丢成 None，
        # 结果 asr_text 永远是空串。别再改回去。
        text = normalize(w.get("w"))
        if not text:
            continue
        start, end = float(w.get("start") or 0.0), float(w.get("end") or 0.0)
        span = max(end - start, 0.0)
        n = len(text)
        for i, ch in enumerate(text):
            out.append({
                "ch": ch,
                "start": round(start + span * i / n, 3),
                "end": round(start + span * (i + 1) / n, 3),
            })
    return out


def _map_to_expected(asr_chars: list[dict], expected: str) -> tuple[list[dict | None], float, float]:
    """把逐字时间戳映射到给定文本的每个字上。对不上的位置是 None，之后插值。

    返回 (时间槽, coverage, ratio)。

    coverage = 期望文本里能在 ASR 输出中按序匹配上的字所占比例。
    用它做判定而不是整体相似度，因为 ASR 在环境音段落上会幻觉
    （实测 a1_c2 开头 2.88 秒吐了一串无关的字）。整体相似度会被这些
    插入字符严重拉低，coverage 不受影响，而真正的念错会把 coverage 拉低。
    """
    asr_text = "".join(c["ch"] for c in asr_chars)
    slots: list[dict | None] = [None] * len(expected)
    matcher = difflib.SequenceMatcher(None, expected, asr_text, autojunk=False)
    matched = 0
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            for offset in range(i2 - i1):
                slots[i1 + offset] = asr_chars[j1 + offset]
    coverage = (matched / len(expected)) if expected else 0.0
    ratio = difflib.SequenceMatcher(None, expected, asr_text).ratio()
    return slots, coverage, ratio


def _interpolate(slots: list[dict | None], expected: str, audio_duration: float) -> list[dict]:
    """给没映射上时间的字做线性插值，保证每个字都有 (start, end)。"""
    n = len(slots)
    known = [i for i, s in enumerate(slots) if s is not None]

    if not known:
        # 一个字都没对上：不能假装对齐成功。均匀摊开，上层会把它报成不匹配。
        step = audio_duration / max(n, 1)
        return [{"ch": ch, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
                for i, ch in enumerate(expected)]

    out: list[dict] = []
    for i in range(n):
        hit = slots[i]
        if hit is not None:
            out.append({"ch": expected[i], "start": hit["start"], "end": hit["end"]})
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:
            out.append({"ch": expected[i], "start": 0.0, "end": slots[nxt]["start"]})
        elif nxt is None:
            out.append({"ch": expected[i], "start": slots[prev]["end"],
                        "end": round(max(audio_duration, slots[prev]["end"]), 3)})
        else:
            a, b = slots[prev], slots[nxt]
            frac = (i - prev) / (nxt - prev)
            t = round(a["end"] + (b["start"] - a["end"]) * frac, 3)
            out.append({"ch": expected[i], "start": t, "end": t})
    return out


def _sentences(expected: str, times: list[dict]) -> list[dict]:
    """按标点断句（逗号顿号也切），用逐字时间给每段取起止。这是字幕和重点句触发的时间来源。

    分段数变了不影响时间：`length` 取的是 `len(normalize(raw))`（标点零长度），
    各段依次把归一化全文消耗完，所以下标永远连续。
    """
    out: list[dict] = []
    cursor = 0
    for raw in split_sentences(expected):
        length = len(normalize(raw))
        if not length:
            continue
        span = times[cursor:cursor + length]
        cursor += length
        if not span:
            continue
        out.append({
            "text": raw,
            "start": round(span[0]["start"], 3),
            "end": round(span[-1]["end"], 3),
        })
    return out


def _diff_note(expected: str, actual: str) -> str:
    sm = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    bits = [
        f"{tag}: 期望 {expected[i1:i2]!r} -> 实际 {actual[j1:j2]!r}"
        for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"
    ]
    return "；".join(bits) or "（无具体差异，但相似度低于阈值）"


def align_shot(cfg, media: Media, project_dir: Path, shot: dict, model) -> dict:
    shot_id = shot["id"]
    clip = artifacts.clip_file(project_dir, shot)
    expected = (shot.get("text") or "").strip()

    if not expected:
        return {
            "shot_id": shot_id,
            "engine": cfg.get("asr.engine"),
            "expected_text": None, "asr_text": None, "match": None,
            "similarity": None,
            "skipped": "该镜头没有 text（空镜/无台词镜头本就不该有语音），不做听检",
            "words": [], "chars": [], "sentences": [],
        }

    # ASR 用的音频放 audio/_asr/，**不要**覆盖 audio/<shot-id>.wav。
    # 后者是交付用音轨（保持原采样率和声道），剪映的音频段和重点句音效都指向它；
    # 拿 16k 单声道去覆盖会悄悄把成片音质降下来。这个坑踩过一次。
    asr_audio = project_dir / "audio" / "_asr" / f"{shot_id}.wav"
    if cfg.get("asr.extract_audio", True) or not asr_audio.is_file():
        media.extract_audio(clip, asr_audio, sample_rate=int(cfg.get("asr.sample_rate", 16000)))

    info = media.probe(clip)
    audio_duration = float(info["duration_sec"] or 0.0)

    segments, _meta = model.transcribe(
        str(asr_audio),
        language=cfg.get("asr.language", "zh"),
        word_timestamps=bool(cfg.get("asr.word_timestamps", True)),
        vad_filter=bool(cfg.get("asr.vad_filter", True)),
        # 只用中性的简体提示词来压繁体输出，**绝不要塞入期望台词**：
        # 那样会诱导模型把期望文本"念"回来，听检就失去意义了。
        initial_prompt=cfg.get("asr.initial_prompt") or None,
    )
    words = [
        {"w": getattr(w, "word", ""),
         "start": round(float(w.start or 0.0), 3),
         "end": round(float(w.end or 0.0), 3)}
        for seg in segments for w in (getattr(seg, "words", None) or [])
    ]

    asr_chars = _chars_from_words(words)
    asr_text = "".join(c["ch"] for c in asr_chars)
    norm_expected = normalize(expected)

    slots, coverage, ratio = _map_to_expected(asr_chars, norm_expected)
    times = _interpolate(slots, norm_expected, audio_duration)

    threshold = float(cfg.get("asr.mismatch_ratio", 0.15))
    matched = coverage >= (1.0 - threshold)

    return {
        "shot_id": shot_id,
        "audio": f"audio/_asr/{shot_id}.wav",
        "engine": cfg.get("asr.engine"),
        "expected_text": expected,
        "asr_text": asr_text,
        "match": matched,
        "coverage": round(coverage, 4),
        "similarity": round(ratio, 4),
        "mismatch_detail": None if matched else _diff_note(norm_expected, asr_text),
        "audio_duration": round(audio_duration, 3),
        "words": words,
        "chars": times,
        "sentences": _sentences(expected, times),
    }


def run(cfg, project_dir: Path, only: list[str] | None = None) -> dict:
    project_dir = Path(project_dir)
    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
    data = artifacts.load_shots(project_dir)

    shots = data["shots"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
        if not shots:
            raise artifacts.ArtifactError(f"only 过滤后没有剩下的镜头：{only}")

    # 模型只加载一次。没有带台词的镜头就完全不加载。
    model = load_model(cfg) if any((s.get("text") or "").strip() for s in shots) else None

    results = []
    for shot in shots:
        payload = align_shot(cfg, media, project_dir, shot, model=model)
        artifacts.write_align(project_dir, shot["id"], payload)
        results.append(payload)
    return {"results": results}


def summarize(results: list[dict]) -> tuple[list[str], list[str]]:
    """返回 (正常行, 不匹配行)，供 CLI 打印。"""
    ok, bad = [], []
    for r in results:
        sid = r["shot_id"]
        if r.get("skipped"):
            ok.append(f"  {sid}: 跳过 —— {r['skipped']}")
        elif r.get("match"):
            ok.append(f"  {sid}: 一致    coverage {r['coverage']}  相似度 {r['similarity']}   "
                      f"字 {len(r.get('chars') or [])} / 句 {len(r.get('sentences') or [])}")
        else:
            bad.append(f"  {sid}: 不一致  coverage {r.get('coverage')}  相似度 {r.get('similarity')}"
                       f"  {r.get('mismatch_detail')}")
    return ok, bad
