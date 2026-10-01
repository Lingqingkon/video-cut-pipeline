"""第 2b 段：AIGC 生成。镜头表 → ComfyUI → clips/

思路
----
生成和超分走**完全相同**的机制（SSH 隧道 → 提交工作流 → 轮询 → 下载产物）。
生成器只负责把镜头翻译成工作流参数，**图结构本身放在 templates/*.json 里**，
不写死在代码里——因为 AutoDL 上是 `MiniMaxH3Director`、本机是 `MiniMaxH3Easy`，
节点名完全不同，写死任何一个换台机器就废。

三条纪律
--------
1. **默认 dry-run**。生视频慢且计费，必须显式 `--yes` 才提交。
2. **幂等**。`clips/<id>.mp4` 已存在就跳过，除非 `--force`。
3. **串行**。一条一条来，每条独立落盘。全塞进队列更快，但第一条参数错了
   后面全跟着错，而且中断了不知道跑到哪。

产物落盘
--------
    ComfyUI output/vcp_gen/<id>_00001_.mp4  →  gen/<id>_00001_.mp4   （原始产物，保留）
                                            →  clips/<id>.mp4        （后续工序用）

先落 gen/ 再复制成 clips/ 是既有惯例：规格化或超分把 clips/ 里的搞坏了还能回退。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import artifacts, comfy, comfyenv

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
SKILL_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = SKILL_ROOT / "templates"


class GenerateError(RuntimeError):
    """继承 RuntimeError，好让 CLI 统一捕获成一行错误。"""


class _Drop:
    """占位符的"这个值不存在"哨兵。

    模板里可以写多个槽位（比如 2 张参考图），实际只有 1 张时，
    生成器给多余槽位传 DROP，替换阶段就会把这个键整条删掉，
    连带把只服务于它的节点也剪掉。这样一份模板能覆盖 1~N 张参考图。
    """

    def __repr__(self) -> str:
        return "DROP"


DROP = _Drop()


# ── 模板 ─────────────────────────────────────────────────────────────────

def resolve_template(cfg, name: str | None = None) -> Path:
    raw = name or cfg.get("generation.template")
    if not raw:
        raise GenerateError("配置里没设 generation.template")
    path = Path(str(raw))
    if not path.is_absolute():
        path = TEMPLATE_DIR / path
    if not path.is_file():
        raise GenerateError(
            f"找不到工作流模板 {path}\n"
            f"模板目录：{TEMPLATE_DIR}（可用的有 "
            f"{[p.name for p in TEMPLATE_DIR.glob('*.json')] if TEMPLATE_DIR.is_dir() else '（目录不存在）'}）"
        )
    return path


def choose_template(cfg, shot: dict) -> tuple[Path, str]:
    """按镜头决定走哪条路，返回 (模板路径, 模式)。

    有参考图（人设图）就走 r2v —— 人物一致性靠参考图，而不是靠把角色描述
    抄进每一条提示词。没有就退回 t2v。

    注意 r2v 的底膜是 ref2va，t2v 是 fl2va，两者不能混；模板里已经写死了对应关系。
    """
    refs = shot.get("ref_images") or []
    if refs:
        return resolve_template(cfg, cfg.get("generation.template_r2v")), "r2v"
    return resolve_template(cfg, cfg.get("generation.template")), "t2v"


def load_template(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GenerateError(f"{path} 顶层必须是对象")
    # 以 _ 开头的键是模板自带的说明，不能提交给 ComfyUI
    # （ComfyUI 会把任意顶层键当成节点，报"节点类型不存在"）
    return {k: v for k, v in data.items() if not k.startswith("_")}


def substitute(value, values: dict, path: str = ""):
    """递归替换 {{key}}。

    整串恰好是一个占位符时**保留原值类型**——数字不能被变成字符串，
    ComfyUI 对 INT/FLOAT 输入收到字符串会报类型错。

    值为 DROP 的键会被整条删掉（见 DROP 的说明）。
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            got = substitute(v, values, f"{path}.{k}")
            if not isinstance(got, _Drop):
                out[k] = got
        return out
    if isinstance(value, list):
        return [substitute(v, values, f"{path}[{i}]") for i, v in enumerate(value)]
    if not isinstance(value, str):
        return value

    whole = PLACEHOLDER.fullmatch(value)
    if whole:
        key = whole.group(1)
        if key not in values:
            raise GenerateError(f"模板用到 {{{{key}}}} 但没有提供值：{key}（位置 {path}）")
        return values[key]

    def repl(match):
        key = match.group(1)
        if key not in values:
            raise GenerateError(f"模板用到 {{{{key}}}} 但没有提供值：{key}（位置 {path}）")
        return str(values[key])

    return PLACEHOLDER.sub(repl, value)


def prune_dropped(graph: dict) -> tuple[dict, list[str]]:
    """剪掉因 DROP 而失效的节点和连线。

    一个节点只要还剩 DROP 值（比如 LoadImage 的 image 被删了），它就没法跑；
    而指向它的连线也必须一起删，否则提交时报"节点不存在"。
    反复剪到稳定，因为剪掉 A 可能让 B 的输入也失效。
    """
    notes: list[str] = []
    removed: set[str] = set()
    while True:
        newly = [nid for nid, node in graph.items()
                 if nid not in removed and not (node.get("inputs") or {})]
        if not newly:
            break
        removed.update(newly)
        for nid in newly:
            notes.append(f"剪掉节点 #{nid} {graph[nid].get('class_type')}（输入被 DROP 掉了）")
        for node in graph.values():
            inputs = node.get("inputs") or {}
            for key in [k for k, v in inputs.items()
                        if isinstance(v, list) and len(v) == 2 and str(v[0]) in removed]:
                inputs.pop(key, None)
                notes.append(f"  #{node.get('class_type')} 的 {key} 指向被剪节点，已删")
    return {k: v for k, v in graph.items() if k not in removed}, notes


def apply_prompt_wrapping(prompt: str, cfg) -> str:
    """把配置里的抑制词缀无条件加到提示词上。

    为什么必须由生成器加、而不是靠镜头表里写：MiniMax-H3 会习惯性地把台词
    烧进画面（它训练数据里这类口播/财经短视频本来就带硬字幕）。靠第 1 段
    每次都记得写是靠不住的——漏一次就出一条带双份字幕的片子。

    已经写过就不重复加，避免同一句在提示词里出现两遍。
    """
    prefix = (cfg.get("generation.prompt_prefix") or "").strip()
    suffix = (cfg.get("generation.prompt_suffix") or "").strip()
    out = prompt
    if prefix and prefix not in out:
        out = f"{prefix}{out}"
    if suffix and suffix not in out:
        out = f"{out}{suffix}"
    return out


# ── 镜头 → 参数 ──────────────────────────────────────────────────────────

def shot_values(cfg, shot: dict, index: int, prefix: str,
                seed_override: int | None = None) -> dict:
    gen = shot.get("gen") or {}

    width = int(gen.get("width") or cfg.get("generation.video.width", 480))
    height = int(gen.get("height") or cfg.get("generation.video.height", 864))
    fps = float(cfg.get("generation.video.fps", 24))
    duration = float(shot["duration"])

    # 帧数是"这条视频多长"的唯一决定项。优先用镜头表里写死的值，
    # 没有就按时长反推（frames = duration × fps）。
    frames = gen.get("frames")
    if frames:
        frames = int(frames)
    else:
        mode = str(cfg.get("generation.video.frames_mode", "auto"))
        if mode != "auto":
            raise GenerateError(f"generation.video.frames_mode 只支持 auto，实际是 {mode!r}")
        frames = int(round(duration * fps))

    seed = gen.get("seed")
    if seed is None:
        seed = int(cfg.get("generation.video.seed_start", 1000)) + index
    if seed_override is not None:
        seed = int(seed_override)

    prompt = (shot.get("prompt") or "").strip()
    if not prompt:
        raise GenerateError(
            f"{shot['id']}: 没有 prompt，无法生成。"
            "AIGC 镜头必须在 shots.json 里写 prompt（第 1 段由模型产出）。"
        )
    prompt = apply_prompt_wrapping(prompt, cfg)

    return {
        # 模型文件
        "unet": cfg.require("generation.models.unet"),
        "unet_ref": cfg.require("generation.models.unet_ref"),
        "clip_name": cfg.require("generation.models.clip"),
        "clip_type": cfg.get("generation.models.clip_type", "minimax"),
        "video_vae": cfg.require("generation.models.video_vae"),
        "audio_vae": cfg.require("generation.models.audio_vae"),
        # 任务型
        "task_type": cfg.get("generation.task_type",
                             "t2v — 文生视频(Text to Video)"),
        "sample_group": cfg.get("generation.sample_group", "采样设置"),
        # 内容与尺寸
        "prompt": prompt,
        "width": width,
        "height": height,
        "ref_max_size": int(gen.get("ref_max_size") or max(width, height)),
        "total_frames": frames,
        # r2v 的 group 用秒数描述时长，和 total_frames 一起给，避免两者打架
        "duration_sec": round(frames / fps, 3),
        "frame_rate": fps,
        "seed": int(seed),
        "cfg": float(cfg.get("generation.video.cfg", 1.0)),
        "steps": int(cfg.get("generation.video.steps", 8)),
        # 参考图（r2v 用）。真正的值在读图上传之后填；这里先给 DROP，
        # 免得模板用到它时报"没有提供值"。
        "ref_image_0": DROP,
        "ref_image_1": DROP,
        # 落盘前缀（ComfyUI 会写成 output/<prefix>_00001_.mp4）
        "prefix": prefix,
    }


# ── 单镜头生成 ────────────────────────────────────────────────────────────

def _upload_refs(client, project_dir: Path, shot: dict, limit: int = 2) -> list[str]:
    """把参考图传进 ComfyUI 的 input/，返回文件名列表。

    两人同框的镜头需要两张（各自的四视图人设图）。插件上限是 9 张，
    这里模板只开了 2 个槽位，要更多得照 I2 的样子加。
    超出上限就**报错**，不要偷偷只传前几张——那样人物一致性看起来"做了"，实际是漏的。
    """
    refs = shot.get("ref_images") or []
    if len(refs) > limit:
        raise GenerateError(
            f"{shot['id']}: 模板最多 {limit} 张参考图，实际给了 {len(refs)} 张。"
            "要更多就在 templates/minimax_h3_director.r2v.json 里加槽位（插件上限 9 张）。"
        )
    names = []
    for i, raw in enumerate(refs):
        p = Path(str(raw))
        path = p if p.is_absolute() else Path(project_dir) / p
        if not path.is_file():
            raise GenerateError(f"{shot['id']}: 参考图不存在：{path}")
        names.append(client.put_input(path, name=f"{shot['id']}_ref{i}{path.suffix.lower()}"))
    return names


def frames_verdict(actual: int, expected: int, quantum: int) -> tuple[str, str | None]:
    """判断实测帧数与请求帧数的关系，返回 (判定, 说明)。

    - `ok`        对得上（±1 帧内 —— 探针按毫秒报时长，本身就有半帧误差）
    - `quantized` **模型把帧数抬到了上一档**。这是 MiniMax-H3 的固定量化行为，不是失败。
                  实测档位等差 17 帧：124 / 141 / 158 / 175 / 192 / 209 / 226 / 243
                  （= 17k+5），请求值一律被抬到**不小于它的最近一档**：
                  120→124、132→141、144→158、204→209、216→226、240→243。
    - `abnormal`  其余一切：比请求短（被截断）、或差了一档以上（参数被无视、被换成别的值）

    为什么不能一律按"差超过 1 帧就报错"：量化是**必然**发生的，报成错误会让每条镜头
    都变成"失败"，退出码全非零（错杀）；而且重试循环会当场短路（见 run()）。
    真正该做的是**承认实测值**：把实测时长回写 shots.json，让排轨跟着真实视频走。
    """
    diff = actual - expected
    if abs(diff) <= 1:
        return "ok", None
    if quantum and 0 < diff < quantum:
        return "quantized", (
            f"实测 {actual} 帧，请求 {expected} 帧 —— 模型把帧数抬到了上一档"
            f"（档位等差 {quantum} 帧，这是模型的固定量化行为，不是失败）"
        )
    return "abnormal", (
        f"实际帧数 {actual} 与请求的 {expected} 不一致（差 {diff:+d} 帧） —— "
        "参数可能没被采纳、total_frames 被节点改写，或产物被截断"
    )


def generate_shot(client, media, cfg, project_dir: Path, shot: dict,
                  index: int, force: bool, seed_override: int | None = None) -> dict:
    shot_id = shot["id"]
    target = project_dir / "clips" / f"{shot_id}.mp4"

    # force 之外，重试时也要能覆盖上一轮产物
    if target.is_file() and not force and seed_override is None:
        return {"shot_id": shot_id, "skipped": f"clips/{shot_id}.mp4 已存在（要重生成加 --force）"}

    template_path, mode = choose_template(cfg, shot)
    values = shot_values(cfg, shot, index, prefix=f"vcp_gen/{shot_id}",
                         seed_override=seed_override)
    if mode == "r2v":
        names = _upload_refs(client, project_dir, shot)
        # 模板有 2 个槽位；只给 1 张时第二个填 DROP，
        # 替换阶段会把那个 LoadImage 节点和指向它的连线一起剪掉。
        values["ref_image_0"] = names[0] if len(names) > 0 else DROP
        values["ref_image_1"] = names[1] if len(names) > 1 else DROP

    graph = substitute(load_template(template_path), values)
    graph, prune_notes = prune_dropped(graph)

    started = time.time()
    prompt_id = client.queue(graph)
    entry = client.wait(prompt_id, timeout=float(cfg.get("generation.timeout", 5400)))
    elapsed = time.time() - started

    files = comfy.collect_files(entry)
    chosen = comfy.pick_output_video(files)
    if chosen is None:
        return {"shot_id": shot_id, "error": "工作流跑完但没找到任何产物", "files": files,
                "prompt_id": prompt_id}

    rel = client.output_rel(chosen)
    # 先落 gen/ 保存原始产物，再复制成 clips/ 给后续工序用
    raw = project_dir / "gen" / Path(chosen["filename"]).name
    size = client.fetch(rel, raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(raw), str(target))

    info = media.probe(target)
    expected_frames = values["total_frames"]
    actual_frames = round(info["duration_sec"] * (info["fps"] or 0))

    result = {
        "shot_id": shot_id,
        "mode": mode,
        "template": template_path.name,
        "out": f"clips/{shot_id}.mp4",
        "raw": f"gen/{raw.name}",
        "resolution": [info["width"], info["height"]],
        "duration_sec": round(info["duration_sec"], 3),
        "frames_actual": actual_frames,
        "frames_requested": expected_frames,
        "has_audio": info["has_audio"],
        "bytes": size,
        "seconds": round(elapsed, 1),
        "seed": values["seed"],
        "comfy_file": rel,
        "prompt_id": prompt_id,
    }
    if prune_notes:
        result["graph_pruned"] = prune_notes

    # 帧数核对。**量化不是失败**，只有"比请求短"或"差了一档以上"才是真异常。
    # 后面排轨用的是 shots.json 里的时长，视频实际长度对不上会让整条时间轴错位，
    # 所以量化这一档也不能放过 —— 但处理方式是"按实测改正 shots.json"，不是报失败。
    quantum = int(cfg.get("generation.video.frame_quantum", 17) or 0)
    verdict, why = frames_verdict(actual_frames, expected_frames, quantum)
    result["frames_verdict"] = verdict
    if verdict == "quantized":
        result["warning"] = why
        clip_fps = float(info["fps"] or 0) or float(cfg.get("generation.video.fps", 24))
        new_duration = round(actual_frames / clip_fps, 3)
        if bool(cfg.get("generation.video.fix_duration", True)):
            # 口径和 vcp footage 一致：**视频说了算**，镜头表反过来对齐它。
            # duration 是排轨的唯一依据（manifest 用 shot["duration"]），必须等于实测。
            try:
                artifacts.patch_shot(project_dir, shot_id, {"duration": new_duration})
                shot["duration"] = new_duration      # 同一轮里的下一次尝试用新值
                result["shots_json_patched"] = {"duration": new_duration}
            except Exception as exc:                 # noqa: BLE001
                # 片子已经生成出来了，不能因为回写失败把这条产物丢掉 —— 但必须报出来，
                # 否则 shots.json 和真实视频不一致，时间轴会静默错位。
                result["shots_json_error"] = f"实测时长回写 shots.json 失败：{exc}"
        else:
            result["warning"] += (f"（generation.video.fix_duration 关着，"
                                  f"shots.json 里还是 {float(shot['duration']):.3f}s，"
                                  f"排轨前要手工改成 {new_duration:.3f}s 或打开开关）")
    elif verdict == "abnormal":
        result["error"] = why

    # 检查模型有没有把台词烧进画面。这是**交付级**问题：成片底部会和我们
    # 在剪映里加的字幕叠成两份，而烧进去的删不掉。
    try:
        from . import textcheck
        m = textcheck.check_clip(cfg, media, target)
        result["textcheck"] = {"score": m["score"], "suspect": m["suspect"],
                               "band_top_pct": m["band_top_pct"],
                               "threshold": m["threshold"]}
    except Exception as exc:      # noqa: BLE001 —— 检测失败不该让整条生成算失败
        result["textcheck"] = {"error": str(exc)}

    return result


# ── 批量 ─────────────────────────────────────────────────────────────────

def plan(cfg, project_dir: Path, only=None) -> list[dict]:
    """列出将要生成什么。dry-run 和真跑都先走这个，保证"看到的"和"跑的"是同一份。"""
    project_dir = Path(project_dir)
    data = artifacts.load_shots(project_dir)
    shots = [s for s in data["shots"] if s.get("source_kind") == "aigc"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    if not shots:
        raise GenerateError("没有 AIGC 镜头可生成（source_kind 必须是 aigc）")

    fps = float(cfg.get("generation.video.fps", 24))
    total_frames = 0
    total_seconds = 0.0
    rows = []
    for i, shot in enumerate(shots):
        exists = (project_dir / "clips" / f"{shot['id']}.mp4").is_file()
        try:
            template_path, mode = choose_template(cfg, shot)
            values = shot_values(cfg, shot, i, prefix=f"vcp_gen/{shot['id']}")
            refs = shot.get("ref_images") or []
            rows.append({"shot_id": shot["id"], "mode": mode,
                         "template": template_path.name,
                         "frames": values["total_frames"], "seed": values["seed"],
                         "ref_images": refs, "ref_count": len(refs),
                         "exists": exists, "error": None})
            total_frames += values["total_frames"]
            total_seconds += values["total_frames"] / fps
        except GenerateError as exc:
            rows.append({"shot_id": shot["id"], "mode": None, "template": None,
                         "frames": None, "seed": None, "ref_image": None,
                         "exists": exists, "error": str(exc)})
    return {"rows": rows, "total_frames": total_frames,
            "total_video_seconds": round(total_seconds, 2)}


def run(cfg, project_dir: Path, only=None, force: bool = False) -> dict:
    project_dir = Path(project_dir)
    media = _media(cfg)
    data = artifacts.load_shots(project_dir)
    shots = [s for s in data["shots"] if s.get("source_kind") == "aigc"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    if not shots:
        raise GenerateError("没有 AIGC 镜头可生成")

    results = []
    info_used: dict = {}
    retries = int(cfg.get("textcheck.retry_on_text", 0) or 0)
    with comfyenv.open_comfy(cfg) as (client, info):
        info_used = info
        for i, shot in enumerate(shots):
            attempts = []
            try:
                # 烧字重试：检出可疑就换个 seed 重来。
                # 实测烧字是概率性的（7 条里 2 条），换个 seed 大概率就干净了。
                # 重试上限由 textcheck.retry_on_text 控制 —— 不设上限会无休止烧 GPU。
                base = ((shot.get("gen") or {}).get("seed")
                        or int(cfg.get("generation.video.seed_start", 1000)) + i)
                for k in range(retries + 1):
                    r = generate_shot(client, media, cfg, project_dir, shot, i,
                                      force if k == 0 else True,
                                      seed_override=None if k == 0 else int(base) + k * 7919)
                    attempts.append(r)
                    suspect = bool((r.get("textcheck") or {}).get("suspect"))
                    # **只在真失败时放弃重试**：提交被拒 / 执行出错 / 跑完没产物。
                    # 这些在 generate_shot 里记成 error；帧数量化只记 warning，
                    # 绝不能让它把循环短路掉 —— 之前就是这么错的：retry_on_text=2
                    # 而实测 retries=0，一次都没重试过。
                    if r.get("error") or r.get("skipped") or not suspect:
                        break
                r = attempts[-1]
                r["text_retries"] = len(attempts) - 1
                if len(attempts) > 1:
                    r["attempts"] = [
                        {"seed": a.get("seed"), "score": (a.get("textcheck") or {}).get("score"),
                         "suspect": (a.get("textcheck") or {}).get("suspect"),
                         "frames_actual": a.get("frames_actual"),
                         "frames_verdict": a.get("frames_verdict")}
                        for a in attempts
                    ]
                results.append(r)
            except (comfy.ComfyError, GenerateError) as exc:
                # 单条失败不影响其余 —— 生视频很贵，一条坏了不该让整批停摆
                results.append({"shot_id": shot["id"], "error": str(exc)})

    report = {
        "backend": info_used,
        "results": results,
    }
    artifacts.write_json(project_dir / "generate.json", report)
    return report


def _media(cfg):
    from .media import Media
    return Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))


def summarize(report: dict) -> tuple[list[str], list[str]]:
    """返回 (正常行, 需要处理的行)。

    疑似烧字的进第二个列表 —— 它不算技术失败，但**是交付级问题**
    （成片底部会和我们加的字幕叠成两份，而烧进去的删不掉）。
    让它进非零退出，就是为了不让它静默流过去。

    帧数量化**不进**第二个列表：它是模型的正常行为，已经按实测回写 shots.json。
    它只作为 `!` 提示行挂在正常行后面，退出码保持 0。
    但"回写 shots.json 失败"进第二个列表 —— 片子是真的，时间轴会静默错位，不能放过。
    """
    ok, bad = [], []
    for r in report.get("results", []):
        sid = r.get("shot_id")
        if r.get("error"):
            bad.append(f"  {sid}: ✗ {r['error']}")
            continue
        if r.get("skipped"):
            ok.append(f"  {sid}: 跳过 —— {r['skipped']}")
            continue

        tc = r.get("textcheck") or {}
        retry = f"  重试{r['text_retries']}次" if r.get("text_retries") else ""
        lines = [(f"  {sid}: [{r.get('mode', '?')}] {r['resolution'][0]}x{r['resolution'][1]}"
                  f"  {r['frames_actual']} 帧 / {r['duration_sec']}s"
                  f"  seed={r['seed']}  {r['seconds']}s"
                  f"  {r['bytes'] / 1024 / 1024:.1f}MB"
                  f"  音轨={'有' if r['has_audio'] else '无'}{retry}")]

        patched = r.get("shots_json_patched") or {}
        if r.get("warning"):
            lines.append(f"      ! {r['warning']}")
        if patched.get("duration") is not None:
            lines.append(f"      ! 已按实测把 shots.json 的 duration 改成 {patched['duration']}s"
                         f"（排轨取的是它）")

        if tc.get("error"):
            lines.append(f"      ⚠ 烧字检测没跑起来：{tc['error']}")
            bad.append("\n".join(lines))
            continue
        if tc.get("suspect"):
            lines.append(f"      ⚠ 疑似画面里烧了文字  score={tc['score']}"
                         f"（阈值 {tc['threshold']}，可疑带在 {tc['band_top_pct']:.0%} 高度）")
            lines.append("      要人工决定：换 seed 重生成（把 textcheck.retry_on_text 调大），"
                         "或确认误报后接受")
            bad.append("\n".join(lines))
            continue

        lines[0] += f"  烧字 score={tc.get('score')}"
        if r.get("shots_json_error"):
            lines.append(f"      ⚠ {r['shots_json_error']}"
                         " —— shots.json 和真实视频不一致，手工对账前不要排轨")
            bad.append("\n".join(lines))
            continue
        ok.append("\n".join(lines))
    return ok, bad
