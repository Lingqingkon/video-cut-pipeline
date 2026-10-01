"""把遗留的 spec_*.json 迁成 shots.json 契约。

这是一次性迁移工具，不是流水线的常设环节。存在的理由：
老数据（spec_a1.json 等）只有 name/prompt/frames/width/height/steps/seed/text/kind，
缺 shots.json 契约要求的 source_kind / shot_kind / duration / file / keyphrase，
而"缺哪条走哪条素材分支"是整条链路的第一个断点，不补上后面全都起不来。

映射规则：
  kind="talk"     -> source_kind=aigc, shot_kind=talk
  kind="vo"       -> source_kind=aigc, shot_kind=broll   （画外音，镜头本身不需要说话）
  其它/缺失        -> source_kind=aigc, shot_kind=broll
  duration        <- frames / fps（生成侧意图值）
  file            <- clips/<name>.mp4
  另外用 ffprobe 实测文件，把实测时长写进 duration 并在 _warnings 里记下与意图值的差
"""

from __future__ import annotations

from pathlib import Path

from . import artifacts
from .media import Media


def _kind_map(kind: str | None) -> tuple[str, str]:
    if kind == "talk":
        return "aigc", "talk"
    if kind == "vo":
        return "aigc", "broll"
    return "aigc", "broll"


def import_spec(project_dir: Path, spec_path: Path, media: Media,
                project_name: str | None = None, task_type: str = "aigc") -> dict:
    spec = artifacts.read_json(spec_path)
    jobs = spec.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise artifacts.ArtifactError(f"{spec_path} 里没有非空的 jobs 数组")

    only = spec.get("only")
    if only:
        wanted = set(only)
        jobs = [j for j in jobs if j.get("name") in wanted]

    project_dir = Path(project_dir)
    clips_dir = project_dir / "clips"

    shots = []
    warnings: list[str] = []

    for job in jobs:
        name = job.get("name")
        if not name:
            warnings.append("跳过一个没有 name 的 job")
            continue

        source_kind, shot_kind = _kind_map(job.get("kind"))

        # 生成侧意图时长
        frames = job.get("frames")
        fps = job.get("fps") or 24
        duration = round(frames / fps, 3) if frames else 0.0

        file_rel = f"clips/{name}.mp4"
        clip = project_dir / file_rel

        # 实测值优先——manifest 里的 duration 就是实测出来的，不是 frames/fps 算的
        if clip.is_file():
            probed = media.probe(clip)
            if probed["duration_sec"]:
                if duration and abs(probed["duration_sec"] - duration) > 0.002:
                    warnings.append(
                        f"{name}: 意图时长 {duration}s（{frames}/{fps}）"
                        f"与实测 {probed['duration_sec']}s 不一致，采用实测值"
                    )
                duration = round(probed["duration_sec"], 3)
        else:
            warnings.append(f"{name}: 找不到 {file_rel}，duration 用意图值 {duration}s")

        shot = {
            "id": name,
            "source_kind": source_kind,
            "shot_kind": shot_kind,
            "duration": duration,
            "text": job.get("text"),
            "prompt": job.get("prompt"),
            "file": file_rel,
            "keyphrase": False,
            "gen": {
                "engine": "minimax_h3",
                "frames": frames,
                "width": job.get("width"),
                "height": job.get("height"),
                "steps": job.get("steps"),
                "seed": job.get("seed"),
            },
        }
        shots.append(shot)

    shots.sort(key=lambda s: s["id"])

    data = {
        "project": project_name or project_dir.name,
        "task_type": task_type,
        "canvas": {
            "width": 1080,
            "height": 1920,
            "fps": fps_of(shots) or 24,
        },
        "characters": [],
        "shots": shots,
        "_migrated_from": str(spec_path),
        "_warnings": warnings,
    }
    return data


def fps_of(shots) -> float:
    for s in shots:
        gen = s.get("gen") or {}
        if gen.get("frames") and s.get("duration"):
            return round(gen["frames"] / s["duration"])
    return 0
