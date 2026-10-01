"""第 4 段：shots.json + align/ → manifest.json

产出的是喂给剪映 MCP 的 `build_draft` 的输入，字段细节以 jianying-ai-mcp 自带的
references/manifest-schema.md 为准。

两条硬规则在这里落地：
  1. **选片段必须走 artifacts.resolved_clip()**，不许直接读 shot["file"]，
     否则会出现"一半镜头超分了、一半没有"而 manifest 里看不出来的半套状态。
  2. **字幕时间只来自 align/*.json**，绝不按镜头时长均分。对齐缺失就报错，
     不许静默退回均分——均分会让字幕和语音错位，而且看不出来是错的。

字幕一律写成独立的 text 段（不用 srt_import）：srt_import 的 style 是整份文件级的，
不能逐句设动画；text 段的 style 是段级的，能力是它的超集，重点句只是多挂一个 animations。
"""

from __future__ import annotations

import copy
from pathlib import Path

from . import artifacts
from .align import normalize, split_sentences


class ManifestError(RuntimeError):
    """继承 RuntimeError，好让 CLI 统一捕获成一行错误而不是甩一堆 traceback。"""
    pass


def fmt(seconds: float) -> str:
    """MCP 接受微秒整数或 "1.5s" 字符串。用字符串，人看得懂。"""
    return f"{max(0.0, float(seconds)):.3f}s"


def _load_keyphrases(project_dir: Path) -> dict[str, list[dict]]:
    """读第 3.5 段解析好的重点句，按 shot_id 分组。

    读的是 `keyphrases_resolved.json` 而不是 `keyphrases.json`：
    前者带着从对齐结果算出来的起止时间和归一化下标，后者只有模型写的文字。
    时间只能来自对齐，不能由模型给。
    """
    path = Path(project_dir) / "keyphrases_resolved.json"
    if not path.is_file():
        return {}
    data = artifacts.read_json(path)
    entries = data.get("keyphrases") if isinstance(data, dict) else data
    out: dict[str, list[dict]] = {}
    for item in entries or []:
        sid = item.get("shot_id")
        if sid and "norm_start" in item:
            out.setdefault(sid, []).append(item)
    return out


def _raw_index(raw: str, norm_offset: int) -> int:
    """把"去掉标点后的第 n 个字符"映射回原字符串里的下标。

    归一化还做了繁→简，但那是逐字替换、长度不变，所以下标可以直接用。
    """
    seen = 0
    for i, ch in enumerate(raw):
        if is_content(ch):
            if seen == norm_offset:
                return i
            seen += 1
    return len(raw)


def is_content(ch: str) -> bool:
    from .align import _is_punct
    return not _is_punct(ch) and not ch.isspace()


def _split_sentence(raw_text: str, norm_lo: int, span: tuple[float, float],
                    inside: list[dict], chars: list[dict], offset: float) -> list[dict]:
    """把一句话按重点句切成若干段。

    只有把重点句切成**独立的 text 段**，才能给它单独挂 animations ——
    animations 是段级的，挂在整句上就不是"重点句触发特效"了。

    **时间边界取自逐字时间戳，不用按比例插值。** 按 8/12 这种比例算出来的边界
    实测能差 0.19 秒（中文语速不均匀，标点处停顿更长），
    而 chars 里本来就有每个字的真实起止。

    `offset` 是镜头在时间轴上的起点。**这个不能省**：`chars` 里是镜头内相对时间，
    而 `span` 传进来的是时间轴绝对时间，不加偏移会拿相对值和绝对值比较，
    结果是整句被夹到句首、三段重叠（t_c1 因为偏移恰好是 0 所以看不出来）。
    """
    start, end = span
    if not inside:
        return [{"text": raw_text, "start": start, "duration": end - start, "kp": None}]

    def t_of(norm_idx: int, which: str) -> float:
        """归一化下标 -> 时间轴绝对时间。越界就退回句子的起止。"""
        if norm_idx < 0 or norm_idx >= len(chars):
            return start if which == "start" else end
        return offset + float(chars[norm_idx]["start" if which == "start" else "end"])

    out: list[dict] = []
    ordered = sorted(inside, key=lambda k: k["norm_start"])
    cursor_norm = norm_lo
    cursor_t = start

    for kp in ordered:
        head_end = _raw_index(raw_text, kp["norm_start"] - norm_lo)
        tail_start = _raw_index(raw_text, kp["norm_start"] + kp["norm_len"] - norm_lo)

        head_text = raw_text[_raw_index(raw_text, cursor_norm - norm_lo):head_end].strip()
        kp_text = raw_text[head_end:tail_start].strip()
        kp_t0 = t_of(kp["norm_start"], "start")
        kp_t1 = t_of(kp["norm_start"] + kp["norm_len"] - 1, "end")
        kp_t0 = min(max(kp_t0, cursor_t), end)
        kp_t1 = min(max(kp_t1, kp_t0 + 0.05), end)

        if head_text:
            out.append({"text": head_text, "start": cursor_t,
                        "duration": max(kp_t0 - cursor_t, 0.05), "kp": None})
        if kp_text:
            out.append({"text": kp_text, "start": kp_t0,
                        "duration": max(kp_t1 - kp_t0, 0.05), "kp": kp})
        cursor_norm = kp["norm_start"] + kp["norm_len"]
        cursor_t = kp_t1

    tail_text = raw_text[_raw_index(raw_text, cursor_norm - norm_lo):].strip()
    if tail_text:
        out.append({"text": tail_text, "start": cursor_t,
                    "duration": max(end - cursor_t, 0.05), "kp": None})
    return out or [{"text": raw_text, "start": start, "duration": end - start, "kp": None}]


def _audio_segment(project_dir: Path, kp: dict, shot_start: float) -> str | None:
    """重点句的音频段。相对路径要转成绝对路径，剪映吃绝对路径。"""
    rel = kp.get("audio_segment")
    if not rel:
        return None
    path = Path(project_dir) / rel
    return str(path.resolve()) if path.is_file() else None


def build(cfg, project_dir: Path, draft_name: str | None = None
          ) -> tuple[dict, dict, list[str], list[str]]:
    """返回 (manifest, stats, warnings, problems)。

    warnings 是提示性的（比如"重点句已挂动画"）；
    problems 是硬错误（字幕重叠、片段缺失、半套超分状态）——这些在剪映里
    只表现为"某段没出现"或"字幕闪一下"，从结果很难反查，所以必须当错误报。
    """
    project_dir = Path(project_dir)
    shots_data = artifacts.load_shots(project_dir)
    shots = shots_data["shots"]
    warnings: list[str] = []
    aligns = artifacts.load_align_all(project_dir)

    # 画布取哪个：**config 的 `jianying.canvas` 才是权威**，shots.json 的 canvas 只当兜底。
    #
    # 为什么必须 config 优先：shots.json 的 canvas 描述的是**生成侧**分辨率（480×864，
    # 由 `generation.video` 决定），而草稿画布是**后期**分辨率。开了超分之后两者**本就不同**：
    # 素材来自 clips_sr/（720×1280），草稿画布也得是 720×1280。
    # 老写法是 shots.json 优先 —— 结果第 2.5 段超分跑到 720×1280、草稿画布却还是
    # 480×864，正好是硬纪律 6 说的**半套状态**（"超分了但画布还是 480"），
    # 而且它在剪映里只表现为"画面被缩小居中"，从结果很难反查。
    #
    # 注意**不要**顺手把 shots.json 的 canvas 改成后期分辨率：那个值还被
    # `vcp footage` 用来做规格化的目标尺寸，改它会连累口播素材的入库。
    canvas_cfg = cfg.get("jianying.canvas") or {}
    canvas_shots = shots_data.get("canvas") or {}
    width = int(canvas_cfg.get("width")
                or canvas_shots.get("width")
                or 1080)
    height = int(canvas_cfg.get("height")
                 or canvas_shots.get("height")
                 or 1920)
    fps = int(canvas_cfg.get("fps")
              or canvas_shots.get("fps")
              or 24)
    # 草稿名优先级：命令行 > 配置 > shots.json 的 project 字段 > 目录名。
    # 用独立名字（比如加 _v2）是为了不覆盖已经存在的手工草稿。
    draft_name = (draft_name
                  or cfg.get("jianying.draft_name")
                  or shots_data.get("project")
                  or project_dir.name)

    sub_style = copy.deepcopy(cfg.get("jianying.subtitle.style") or {"size": 8, "align": 1})
    sub_border = copy.deepcopy(cfg.get("jianying.subtitle.border") or
                               {"color": [0, 0, 0], "width": 20})
    sub_clip = copy.deepcopy(cfg.get("jianying.subtitle.clip") or {"transform_y": -0.7})

    keyphrases = _load_keyphrases(project_dir)

    video_segments: list[dict] = []
    text_segments: list[dict] = []
    audio_segments: list[dict] = []
    cursor = 0.0
    upscaled_used = 0
    missing_sr: list[str] = []

    for shot in shots:
        sid = shot["id"]
        duration = float(shot["duration"])
        clip = artifacts.resolved_clip(project_dir, shot, cfg)

        if not clip.is_file():
            raise ManifestError(
                f"{sid}: 找不到片段 {clip}。"
                f"（upscale.enabled={cfg.get('upscale.enabled')}）"
            )
        if clip.parent.name == "clips_sr":
            upscaled_used += 1
        elif cfg.get("upscale.enabled") and not (clip.parent.parent / "clips_sr" / f"{sid}.mp4").is_file():
            missing_sr.append(sid)

        # 视频段必须**首尾严格相接**，不能有 1 毫秒的重叠或空隙。
        #
        # 做法：把这一段的两个端点各自吸附到毫秒，再用端点差算时长。
        # 为什么不能分别四舍五入 start 和 duration：cursor 是精确浮点累加的，
        # 而 start/duration 各自 round 到毫秒，两者误差方向不同就会出现
        # 「上一段的 end 比下一段的 start 大 1 毫秒」。
        # 剪映 build_draft 对重叠是零容忍（实测报 SegmentOverlap：
        # 某项目 c9 渲染成 [63.292, 70.584]，而 c10 精确起点是 70.583）。
        # 用端点差算时长之后，段 i 的 end 和段 i+1 的 start 是同一个整数，
        # 从构造上就不可能重叠。
        start_ms = round(cursor * 1000)
        end_ms = round((cursor + duration) * 1000)
        video_segments.append({
            "source": str(clip.resolve()),
            "start": f"{start_ms / 1000:.3f}s",
            "duration": f"{(end_ms - start_ms) / 1000:.3f}s",
        })

        # ── 字幕：时间只来自对齐结果 ──────────────────────────────────
        # 没有台词的镜头（B-roll、空镜）本来就不该有语音，所以不要求对齐。
        # 强制要求的话，纯混剪的片子会因为"没有语音可对齐"直接跑不动。
        has_text = bool((shot.get("text") or "").strip())
        align = aligns.get(sid)
        if align is None:
            if has_text:
                raise ManifestError(
                    f"{sid}: 有台词但没有 align/{sid}.json，无法确定字幕时间。\n"
                    f"先跑 `vcp align {project_dir}`。"
                    "不要用镜头时长均分来凑——那会让字幕和语音错位，而且从结果上看不出来。"
                )
            cursor += duration
            continue
        if align.get("skipped"):
            cursor += duration
            continue

        # 这个镜头的重点句（可能多条）。带 norm_start/norm_len 用来在句子里精确切分。
        kps = keyphrases.get(sid) or []

        # 句子的归一化下标要重算一遍：align 只给了时间和原文，
        # 切重点句需要"这个句子在整条台词的归一化文本里占哪一段"。
        raw_sentences = split_sentences(align.get("expected_text") or "")
        sentences = align.get("sentences") or []
        norm_cursor = 0

        for idx, sentence in enumerate(sentences):
            raw_text = (raw_sentences[idx] if idx < len(raw_sentences)
                        else sentence.get("text") or "")
            nlen = len(normalize(raw_text))
            norm_lo, norm_hi = norm_cursor, norm_cursor + nlen
            norm_cursor += nlen

            start = cursor + float(sentence["start"])
            end = cursor + float(sentence["end"])
            if end <= start:
                warnings.append(f"{sid}: 句「{sentence['text']}」的起止时间非法，跳过")
                continue
            # 夹在镜头时间范围内，防止对齐误差把字幕顶到下一个镜头
            start = max(cursor, min(start, cursor + duration))
            end = max(start + 0.05, min(end, cursor + duration))

            # 落在这一句里的重点句，按归一化下标切成 [前][重点][后] 三段。
            # 只有 standalone 的才切 —— 不切的话动画只能挂整句。
            inside = [
                k for k in kps
                if k.get("standalone", True)
                and norm_lo <= k["norm_start"]
                and k["norm_start"] + k["norm_len"] <= norm_hi
            ]
            pieces = _split_sentence(raw_text, norm_lo, (start, end), inside,
                                     align.get("chars") or [], cursor)

            for piece in pieces:
                seg: dict = {
                    "text": piece["text"],
                    "start": fmt(piece["start"]),
                    "duration": fmt(piece["duration"]),
                    "style": copy.deepcopy(sub_style),
                    "border": copy.deepcopy(sub_border),
                    "clip": copy.deepcopy(sub_clip),
                }
                kp = piece.get("kp")
                if kp:
                    if kp.get("style"):
                        seg["style"].update(copy.deepcopy(kp["style"]))
                    if kp.get("animation"):
                        seg["animations"] = [copy.deepcopy(kp["animation"])]
                    if kp.get("note"):
                        warnings.append(f"{sid}: 重点句「{piece['text']}」已挂动画 —— {kp['note']}")
                    else:
                        warnings.append(f"{sid}: 重点句「{piece['text']}」已挂动画")
                text_segments.append(seg)

        # 重点句的音效段。effects 只能挂音频段，所以这些段是从画面里切出来的。
        for kp in kps:
            if not kp.get("sfx"):
                continue
            segment = _audio_segment(project_dir, kp, cursor)
            if segment is None:
                warnings.append(f"{sid}: 重点句「{kp['text']}」没有音频段，音效挂不上")
                continue
            audio_segments.append({
                "source": segment,
                "start": fmt(cursor + float(kp["audio_start"])),
                "duration": fmt(float(kp["audio_end"]) - float(kp["audio_start"])),
                "volume": float(cfg.get("keyphrase.sfx_volume", 1.0)),
                "effects": [copy.deepcopy(kp["sfx"])],
            })

        cursor += duration

    tracks: list[dict] = []
    if video_segments:
        tracks.append({
            "type": "video",
            "name": str(cfg.get("jianying.video_track_name") or "主画面"),
            "segments": video_segments,
        })
    if text_segments:
        tracks.append({
            "type": "text",
            "name": str(cfg.get("jianying.subtitle.track_name") or "字幕"),
            "segments": text_segments,
        })
    if audio_segments:
        tracks.append({
            "type": "audio",
            "name": str(cfg.get("keyphrase.sfx_track_name") or "重点句音效"),
            "segments": audio_segments,
        })

    manifest = {
        "draft": {"name": draft_name, "width": width, "height": height, "fps": fps},
        "tracks": tracks,
    }

    problems = _self_check(video_segments, text_segments, cursor)
    if cfg.get("upscale.enabled") and missing_sr:
        problems.append(
            "upscale.enabled 是 true，但这些镜头没有 clips_sr 产物，实际用了原始片段："
            + "、".join(missing_sr)
            + " —— 这就是半套状态，要么补齐超分，要么把开关关掉。"
        )

    # 反向的半套状态：超分开着、素材已经是 clips_sr/，但画布尺寸和超分目标对不上。
    # 这在剪映里只表现为"画面被缩小居中"（720×1280 的素材放进 480×864 的画布），
    # 从结果反查不出来，所以必须当硬错误报出来。
    # 画布必须等于**实测**素材分辨率 —— 不信任何配置开关，直接探针量第一条素材。
    # 这一条不依赖 upscale.enabled，所以能抓住所有方向的半套状态：
    # 超分了画布没跟上、改了生成分辨率但素材还是旧的、手动换过文件……全都跑不掉。
    # 为什么必须当硬错误：它在剪映里只表现为"画面被缩小居中"，从结果反查不出来。
    if video_segments:
        try:
            from .media import Media
            _m = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
            _first = Path(str(video_segments[0]["source"]))
            _info = _m.probe(_first)
            aw, ah = int(_info["width"] or 0), int(_info["height"] or 0)
            if aw and ah and (aw, ah) != (width, height):
                problems.append(
                    f"画布 {width}×{height} 和素材实测分辨率 {aw}×{ah} 不一致"
                    f"（{_first.name}）—— 半套状态。"
                    f"素材是多少画布就得是多少：要么把 jianying.canvas 改回 {aw}×{ah}，"
                    "要么把 generation.video 的宽高改成画布尺寸后**重新生成**，"
                    "或者打开超分并让 upscale.target 等于画布。"
                )
        except Exception as exc:      # noqa: BLE001 —— 探针失败不该让 manifest 崩掉
            warnings.append(f"没能实测素材分辨率（{exc}）：画布和素材是否一致未验证")

    if cfg.get("upscale.enabled"):
        tw = int(cfg.get("upscale.target_width", 0) or 0)
        th = int(cfg.get("upscale.target_height", 0) or 0)
        if tw and th and (width, height) != (tw, th):
            problems.append(
                f"画布 {width}×{height} 和超分目标 {tw}×{th} 不一致 —— 这是半套状态。"
                "硬纪律 6：`jianying.canvas` 和 `upscale.enabled` 必须一起改。"
                f"把 config.yaml 的 jianying.canvas 改成 {tw}×{th}，"
                "或把 upscale.enabled 关掉。"
            )

    stats = {
        "shot_count": len(shots),
        "video_segments": len(video_segments),
        "text_segments": len(text_segments),
        "total_seconds": round(cursor, 3),
        "upscaled_clips_used": upscaled_used,
        "keyphrase_shots": sorted(keyphrases),
    }
    return manifest, stats, warnings, problems


def _self_check(video_segments: list[dict], text_segments: list[dict],
                total: float) -> list[str]:
    out: list[str] = []
    if not video_segments:
        out.append("视频轨是空的")
    if not text_segments:
        out.append("字幕轨是空的")

    def to_sec(value) -> float:
        if isinstance(value, (int, float)):
            return float(value) / 1_000_000 if value > 100000 else float(value)
        return float(str(value).rstrip("s"))

    # 字幕不能互相重叠，否则剪映里会出现两条同时显示的字幕
    ordered = sorted(text_segments, key=lambda s: to_sec(s["start"]))
    for prev, cur in zip(ordered, ordered[1:]):
        prev_end = to_sec(prev["start"]) + to_sec(prev["duration"])
        if to_sec(cur["start"]) < prev_end - 0.001:
            out.append(
                f"字幕重叠：「{prev['text'][:12]}」到 {prev_end:.3f}s，"
                f"「{cur['text'][:12]}」从 {to_sec(cur['start']):.3f}s 开始"
            )

    covers = sum(to_sec(s["duration"]) for s in video_segments)
    if abs(covers - total) > 0.01:
        out.append(f"视频轨总时长 {covers:.3f}s 与累加游标 {total:.3f}s 不一致")
    return out


def run(cfg, project_dir: Path, out_name: str = "manifest.json",
        draft_name: str | None = None) -> dict:
    project_dir = Path(project_dir)
    # stats 只用于打印，**不写进文件**：build_draft 会校验 schema，
    # 顶上多一个自定义键有被拒的风险。
    manifest, stats, warnings, problems = build(cfg, project_dir, draft_name=draft_name)

    # **有硬错误就不写盘。** 老写法是无条件写，然后 CLI 再打印"没有生成可用的 manifest" ——
    # 三处都错：① 工具自相矛盾（说没生成，其实写了）；② 把上一次那份好的 manifest.json
    # 覆盖掉了（踩过：改画布后跑一次失败，480p 那份记录就没了）；
    # ③ 下游 build_draft 有可能把这份坏的捡走。
    # 要看坏在哪，CLI 会把 problems 全部打出来，不需要靠落盘。
    if problems:
        return {"path": None, "manifest": manifest, "stats": stats,
                "warnings": warnings, "problems": problems}

    out = project_dir / out_name
    artifacts.write_json(out, manifest)
    return {"path": out, "manifest": manifest, "stats": stats,
            "warnings": warnings, "problems": problems}
