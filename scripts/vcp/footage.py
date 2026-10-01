"""第 2a' 段：口播实拍素材入库。

和混剪（`stock.py`）的关键差别
------------------------------
| | 混剪 stock | 口播 footage |
|---|---|---|
| 音轨 | **丢掉**（当 B-roll，旁白压在上面） | **必须保留**（人声就是内容本身） |
| 时长 | 截到镜头需要的长度 | **不截**（视频本身就是时间基准） |
| 时长来源 | 镜头表说了算 | **视频说了算**，镜头表要反过来对齐它 |

最后一条是这一步存在的核心理由：口播的镜头时长是由"人说了多久"决定的，
不是预定的。如果 `shots.json` 里的 duration 和实拍视频对不上，
第 3 段的对齐、第 4 段的时间轴、字幕全都会错位——而且错得看不出来（
表现为"字幕整体前移/后移一点"）。所以这里必须核对并报出来。
"""

from __future__ import annotations

from pathlib import Path

from . import artifacts


class FootageError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm")


def find_source(project_dir: Path, shot: dict, explicit: Path | None) -> Path:
    """确定这个镜头的实拍源文件。

    优先用 --source 指定的；没指定就在约定的 incoming/ 目录里按镜头 id 找。
    约定目录的意义：一次导入一批镜头时不用逐个敲参数。
    """
    if explicit is not None:
        if not explicit.is_file():
            raise FootageError(f"--source 指向的文件不存在：{explicit}")
        return explicit

    incoming = Path(project_dir) / "incoming"
    if incoming.is_dir():
        for ext in VIDEO_EXTS:
            cand = incoming / f"{shot['id']}{ext}"
            if cand.is_file():
                return cand
        hits = [p for p in sorted(incoming.iterdir())
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise FootageError(
                f"incoming/ 里有多个视频（{[p.name for p in hits]}），"
                f"用 --source 指定，或把文件重命名成 {shot['id']}.mp4"
            )
    raise FootageError(
        f"{shot['id']}: 找不到实拍源文件。\n"
        f"  要么用 --source 指定，要么把它放到 {incoming}\\ 并命名成 {shot['id']}.mp4"
    )


def footage_shots(project_dir: Path, only=None) -> list[dict]:
    data = artifacts.load_shots(project_dir)
    shots = [s for s in data["shots"] if s.get("source_kind") == "footage"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    return shots


def import_one(cfg, media, project_dir: Path, shot: dict, source: Path,
               force: bool, fix_duration: bool) -> dict:
    sid = shot["id"]
    target = project_dir / "clips" / f"{sid}.mp4"
    if target.is_file() and not force:
        return {"shot_id": sid, "skipped": f"clips/{sid}.mp4 已存在（要覆盖加 --force）"}

    src_info = media.probe(source)
    if not src_info["has_audio"]:
        raise FootageError(
            f"{sid}: 源文件没有音轨，口播必须有声音。\n"
            f"  {source}\n"
            "如果这是无声素材，应该走混剪（source_kind: stock）而不是口播。"
        )

    normalized = bool(cfg.get("footage.normalize", True))
    if normalized:
        data = artifacts.load_shots(project_dir)
        canvas = data.get("canvas") or {}
        cw = int(canvas.get("width") or cfg.get("jianying.canvas.width", 480))
        ch = int(canvas.get("height") or cfg.get("jianying.canvas.height", 864))
        cfps = float(canvas.get("fps") or cfg.get("jianying.canvas.fps", 24))
        _normalize(media, source, target, cw, ch, cfps)
    else:
        import shutil
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))

    out_info = media.probe(target)

    # 时长必须核对：口播的时长由视频决定，镜头表写错了整条时间轴都会错位
    plan_dur = float(shot["duration"])
    real_dur = float(out_info["duration_sec"])
    drift = real_dur - plan_dur
    result = {
        "shot_id": sid,
        "source": str(source),
        "out": f"clips/{sid}.mp4",
        "normalized": normalized,
        "source_resolution": [src_info["width"], src_info["height"]],
        "out_resolution": [out_info["width"], out_info["height"]],
        "duration_planned": round(plan_dur, 3),
        "duration_actual": round(real_dur, 3),
        "duration_drift": round(drift, 3),
        "has_audio": out_info["has_audio"],
        "bytes": target.stat().st_size,
    }

    # 超过一帧就算对不上。一帧在 24fps 下是 0.0417 秒。
    if abs(drift) > 1.0 / 24 + 0.001:
        if fix_duration:
            artifacts.patch_shot(project_dir, sid, {"duration": round(real_dur, 3)})
            result["duration_fixed_to"] = round(real_dur, 3)
        else:
            result["warning"] = (
                f"镜头表写的是 {plan_dur:.3f}s，实拍是 {real_dur:.3f}s，"
                f"差 {drift:+.3f}s。"
                "不修正的话整条时间轴会错位（表现为字幕整体偏移，很难反查）。"
                "加 --fix-duration 自动改成实测值。"
            )
    return result


def _normalize(media, src: Path, dst: Path, width: int, height: int, fps: float) -> None:
    """缩放到画布尺寸并统一帧率，**保留音轨**。

    用 force_original_aspect_ratio=increase + crop（铺满后居中裁切），不拉伸。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},fps={fps:g}")
    media._run([
        media.ffmpeg, "-v", "error", "-y", "-i", str(src),
        "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k", str(dst),
    ])


def summarize(results: list[dict]) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for r in results:
        sid = r.get("shot_id")
        if r.get("error"):
            bad.append(f"  {sid}: ✗ {r['error']}")
            continue
        if r.get("skipped"):
            ok.append(f"  {sid}: 跳过 —— {r['skipped']}")
            continue
        note = ""
        if r.get("duration_fixed_to") is not None:
            note = f"  （时长已从 {r['duration_planned']}s 改成 {r['duration_fixed_to']}s）"
        ok.append(f"  {sid}: {r['source_resolution'][0]}x{r['source_resolution'][1]}"
                  f" → {r['out_resolution'][0]}x{r['out_resolution'][1]}"
                  f"  {r['duration_actual']}s"
                  f"  {r['bytes'] / 1024 / 1024:.1f}MB"
                  f"  音轨={'有' if r['has_audio'] else '无'}{note}")
        if r.get("warning"):
            ok.append(f"      ! {r['warning']}")
    return ok, bad
