"""第 2.5 段：超分。

生成侧出的是 480×864（0.4MP），画布是 1080×1920，差 2.25 倍，不处理一定糊。

实现走 ComfyUI 的 Real-ESRGAN：
    LoadVideo → GetVideoComponents → ImageUpscaleWithModel(x4) → ImageScale(裁到目标)
              → CreateVideo(画面 + 原音轨) → SaveVideo

**比例问题**：480×864 是 5:9（0.5556），画布 1080×1920 是 9:16（0.5625）。
ResolutionSelector 取 32 的整数倍，所以宽度是 480 而不是 486。差 1.25%。
用 ImageScale 的 crop=center（先按比例放大填满再居中裁切）处理，不做拉伸。

产物写 clips_sr/，**不覆盖 clips/**：超分是可以重跑的后期步骤，
原始片段必须留着，否则换模型或调参数就得重新生成。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import artifacts, comfy, comfyenv


def build_graph(clip_name: str, model: str, width: int, height: int,
                method: str, crop: str, prefix: str) -> dict:
    """ComfyUI API 格式（不是界面格式）。节点输入名以 /object_info 为准。"""
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": clip_name}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "4": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["3", 0], "image": ["2", 0]}},
        "5": {"class_type": "ImageScale",
              "inputs": {"image": ["4", 0], "upscale_method": method,
                         "width": width, "height": height, "crop": crop}},
        "6": {"class_type": "CreateVideo",
              "inputs": {"images": ["5", 0], "fps": ["2", 2], "audio": ["2", 1]}},
        "7": {"class_type": "SaveVideo",
              "inputs": {"video": ["6", 0], "filename_prefix": prefix,
                         "format": "auto", "codec": "auto"}},
    }


def _pick_video(files: list[dict]) -> dict | None:
    """挑产物。逻辑放在 comfy.pick_output_video，生成阶段共用同一份。"""
    return comfy.pick_output_video(files)


def upscale_shot(client, media, project_dir: Path, shot: dict, cfg) -> dict:
    shot_id = shot["id"]
    src = artifacts.clip_file(project_dir, shot)
    if not src.is_file():
        return {"shot_id": shot_id, "skipped": f"找不到源片段 {shot.get('file')}"}

    info = media.probe(src)
    target_w = int(cfg.get("upscale.target_width", 1080))
    target_h = int(cfg.get("upscale.target_height", 1920))

    if (info["width"], info["height"]) == (target_w, target_h):
        return {"shot_id": shot_id, "skipped": "源片段已是目标分辨率"}

    model = cfg.require("upscale.model")
    prefix = f"vcp_sr/{shot_id}"
    clip_name = client.put_input(src, name=f"{shot_id}{src.suffix}")

    graph = build_graph(
        clip_name=clip_name,
        model=model,
        width=target_w,
        height=target_h,
        method=str(cfg.get("upscale.method", "lanczos")),
        crop=str(cfg.get("upscale.crop", "center")),
        prefix=prefix,
    )

    started = time.time()
    prompt_id = client.queue(graph)
    entry = client.wait(prompt_id, timeout=float(cfg.get("upscale.timeout", 3600)))
    elapsed = time.time() - started

    files = comfy.collect_files(entry)
    chosen = _pick_video(files)
    if chosen is None:
        return {"shot_id": shot_id, "error": "工作流跑完但没找到任何产物文件",
                "files": files}

    rel = client.output_rel(chosen)
    dest = project_dir / "clips_sr" / f"{shot_id}.mp4"
    size = client.fetch(rel, dest)
    out_info = media.probe(dest)

    # 断言产物真的变成目标分辨率。没有这道检查的话，拿到输入原片回显
    # 也会显示"成功"，而分辨率根本没变。
    actual = (out_info["width"], out_info["height"])
    if actual != (target_w, target_h):
        return {
            "shot_id": shot_id,
            "error": f"超分产物分辨率是 {actual[0]}x{actual[1]}，不是目标 "
                     f"{target_w}x{target_h}。挑到的文件可能是 {rel}",
            "comfy_file": rel, "prompt_id": prompt_id,
        }

    return {
        "shot_id": shot_id,
        "src": str(src.relative_to(project_dir)),
        "src_resolution": [info["width"], info["height"]],
        "out": f"clips_sr/{shot_id}.mp4",
        "out_resolution": [out_info["width"], out_info["height"]],
        "has_audio": out_info["has_audio"],
        "duration_sec": round(out_info["duration_sec"], 3),
        "bytes": size,
        "seconds": round(elapsed, 1),
        "comfy_file": rel,
        "prompt_id": prompt_id,
    }


def run(cfg, project_dir: Path, only: list[str] | None = None) -> dict:
    project_dir = Path(project_dir)
    media = _media(cfg)
    data = artifacts.load_shots(project_dir)

    shots = data["shots"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
        if not shots:
            raise artifacts.ArtifactError(f"only 过滤后没有剩下的镜头：{only}")

    # 这里原本是硬拦（配置说关着就不许跑）。改成提醒的原因：
    # 默认值已经是 False，硬拦会让"没写 upscale 段的项目"根本跑不了这一命令；
    # 而"超分了但画布没跟上"这个真正的风险，manifest 现在会用**实测分辨率**抓到，
    # 比在这里猜配置可靠。

    results = []
    info_used: dict = {}
    with comfyenv.open_comfy(cfg) as (client, info):
        info_used = info
        for shot in shots:
            results.append(upscale_shot(client, media, project_dir, shot, cfg))

    report = {
        "backend": info_used,
        "model": cfg.get("upscale.model"),
        "target": [int(cfg.get("upscale.target_width", 1080)),
                   int(cfg.get("upscale.target_height", 1920))],
        "results": results,
    }
    artifacts.write_json(project_dir / "upscale.json", report)
    return report


def _media(cfg):
    from .media import Media
    return Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))


def summarize(report: dict) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for r in report.get("results", []):
        sid = r.get("shot_id")
        if r.get("skipped"):
            ok.append(f"  {sid}: 跳过 —— {r['skipped']}")
        elif r.get("error"):
            bad.append(f"  {sid}: ✗ {r['error']}")
        else:
            ok.append(
                f"  {sid}: {r['src_resolution'][0]}x{r['src_resolution'][1]}"
                f" -> {r['out_resolution'][0]}x{r['out_resolution'][1]}"
                f"  {r['seconds']}s  {r['bytes'] / 1024 / 1024:.1f}MB"
                f"  音轨={'有' if r['has_audio'] else '无'}"
            )
    return ok, bad
