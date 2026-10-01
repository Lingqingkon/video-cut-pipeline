"""第 2a 段：混剪素材。镜头表 → Pexels 检索下载 → 规格化 → clips/

职责划分
--------
检索和下载复用现成的 `stockvid`（它已经能跑，而且做了下载地址 HEAD 校验、
去音轨、写边车 JSON）。这一步只补两件它不管的事：

1. **挑哪一个。** 检索回来是一堆候选，得按镜头需求选一条。
2. **规格化。** 素材站回来的是任意分辨率、任意时长、任意帧率。
   画布是 480×864（5:9），素材可能是 1080×1920（9:16），直接丢进剪映会被拉伸或留边。
   这里统一裁切+缩放到画布尺寸，并把时长截到镜头需要的长度，
   这样下游（超分、manifest）拿到的片段和 AIGC 生成的完全一致，不用分支处理。

一个实测坑
----------
`stockvid download` 返回的文件名是**候选元数据里的分辨率**，不等于真实文件。
实测有一个候选标 1080x1920、文件名也写 1080x1920，但下载下来实际是 720x1280。
所以判定一律用返回的 `width`/`height`/`duration` 字段，**不要从文件名里解析**。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import artifacts

SKILL_ROOT = Path(__file__).resolve().parents[2]


class StockError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


# ── 调 stockvid ──────────────────────────────────────────────────────────

def _fetcher_script(cfg) -> Path:
    raw = cfg.get("stock.fetcher_dir")
    if not raw:
        raise StockError("配置里没设 stock.fetcher_dir（stockvid 所在的目录）")
    script = Path(str(raw)) / "stockvid.py"
    if not script.is_file():
        raise StockError(
            f"找不到 {script}\n"
            "stock.fetcher_dir 要指向 stock-video-fetcher 目录（里面有 stockvid.py）。"
        )
    return script


def _run_stockvid(cfg, script: Path, args: list[str], timeout: float) -> object:
    """跑 stockvid 并把 --json 的输出解析回来。

    注意：stockvid 的叙述走 stderr，JSON 走 stdout。所以这里只解析 stdout，
    stderr 留着在出错时一起报出来。
    """
    python = cfg.get("stock.python") or sys.executable
    cmd = [str(python), str(script)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(script.parent))
    if proc.returncode != 0:
        raise StockError(
            f"stockvid 退出码 {proc.returncode}：{' '.join(args[:3])} …\n"
            f"{(proc.stderr or proc.stdout or '')[-900:]}"
        )
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise StockError(
            f"stockvid 的输出不是合法 JSON：{exc}\n"
            f"stdout 前 400 字：{(proc.stdout or '')[:400]}"
        ) from exc


def search(cfg, query: str) -> list[dict]:
    script = _fetcher_script(cfg)
    args = ["search", query, "--json",
            "--limit", str(int(cfg.get("stock.search_limit", 12))),
            "--orientation", str(cfg.get("stock.orientation", "portrait")),
            "--target-height", str(int(cfg.get("stock.target_height", 1280)))]
    provider = cfg.get("stock.provider")
    if provider and provider != "auto":
        args += ["--provider", str(provider)]
    if cfg.get("stock.min_short_side"):
        args += ["--min-short-side", str(int(cfg.get("stock.min_short_side")))]
    if cfg.get("stock.strict"):
        args.append("--strict")
    result = _run_stockvid(cfg, script, args, float(cfg.get("stock.timeout", 300)))
    if not isinstance(result, list):
        raise StockError(f"检索结果不是数组：{type(result).__name__}")
    return result


def download(cfg, candidate: dict, out_dir: Path) -> dict:
    """下载一条候选，返回 stockvid 的下载结果（含真实 path/width/height/duration）。"""
    script = _fetcher_script(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.get("stock.keep_audio", False):
        pass  # stockvid 默认就去音轨，不用加参数
    args = ["download", "--from-json", "-", "--out", str(out_dir), "--json"]
    if cfg.get("stock.keep_audio", False):
        args.append("--keep-audio")

    python = cfg.get("stock.python") or sys.executable
    proc = subprocess.run(
        [str(python), str(script)] + args,
        input=json.dumps([candidate], ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=float(cfg.get("stock.download_timeout", 600)),
        cwd=str(script.parent),
    )
    if proc.returncode != 0:
        raise StockError(
            f"下载失败，退出码 {proc.returncode}\n{(proc.stderr or proc.stdout or '')[-900:]}"
        )
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise StockError(f"下载返回的不是合法 JSON：{exc}") from exc
    if not rows:
        raise StockError("下载没有返回任何条目")
    row = rows[0]
    if not row.get("ok"):
        raise StockError(f"下载失败：{row.get('error') or row}")
    return row


# ── 挑选 ─────────────────────────────────────────────────────────────────

def score(candidate: dict, need_duration: float, canvas_h: int) -> float:
    """给候选打分。越高越合适。

    这几项是按"能直接用、少浪费"排的：
      - 时长够不够（够不上的话得循环，观感差）
      - 不用放大（素材高度 >= 画布高度，避免糊）
      - 时长越接近需求越省（不用下无谓的大文件）
    """
    height = candidate.get("height") or 0
    duration = float(candidate.get("duration") or 0)
    size = (candidate.get("selected_file") or {}).get("size_bytes") or 0

    s = 0.0
    if candidate.get("orientation") == "portrait":
        s += 100
    if height and canvas_h and height >= canvas_h:
        s += 50
    if duration >= need_duration:
        s += 100
        s -= (duration - need_duration) * 1.5      # 够用就行，别下太长的
    else:
        s -= (need_duration - duration) * 20       # 不够长比较严重，但仍有候选
    s -= size / 1e7                                # 大文件略降权
    return s


def pick(candidates: list[dict], need_duration: float, canvas_h: int) -> dict:
    if not candidates:
        raise StockError("检索没有返回任何候选")
    usable = [c for c in candidates if (c.get("url_checked") is not False)]
    if not usable:
        raise StockError("所有候选的下载地址都没通过校验")
    return max(usable, key=lambda c: score(c, need_duration, canvas_h))


# ── 规格化 ───────────────────────────────────────────────────────────────

def normalize(media, src: Path, dst: Path, duration: float,
              width: int, height: int, fps: float, keep_audio: bool) -> None:
    """裁切+缩放到画布尺寸，并把时长截到镜头需要的长度。

    用 force_original_aspect_ratio=increase + crop 实现"铺满后居中裁切"，
    而不是拉伸 —— 素材是 9:16、画布是 5:9，直接拉伸会变形。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},fps={fps:g}")
    cmd = [media.ffmpeg, "-v", "error", "-y",
           "-i", str(src), "-t", f"{duration:.3f}", "-vf", vf]
    if not keep_audio:
        cmd.append("-an")
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(dst)]
    media._run(cmd)


# ── 批量 ─────────────────────────────────────────────────────────────────

def plan(cfg, project_dir: Path, only=None) -> list[dict]:
    project_dir = Path(project_dir)
    data = artifacts.load_shots(project_dir)
    shots = [s for s in data["shots"] if s.get("source_kind") == "stock"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    rows = []
    for shot in shots:
        exists = (project_dir / "clips" / f"{shot['id']}.mp4").is_file()
        query = ((shot.get("stock") or {}).get("query") or "").strip()
        rows.append({
            "shot_id": shot["id"],
            "query": query,
            "duration": float(shot["duration"]),
            "exists": exists,
            "error": None if query else "缺 stock.query —— 混剪镜头必须写检索关键词",
        })
    if not rows:
        raise StockError("没有 source_kind=stock 的镜头")
    return rows


def run(cfg, project_dir: Path, only=None, force: bool = False) -> dict:
    project_dir = Path(project_dir)
    from .media import Media
    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))

    data = artifacts.load_shots(project_dir)
    canvas = data.get("canvas") or {}
    cw = int(canvas.get("width") or cfg.get("jianying.canvas.width", 480))
    ch = int(canvas.get("height") or cfg.get("jianying.canvas.height", 864))
    cfps = float(canvas.get("fps") or cfg.get("jianying.canvas.fps", 24))

    shots = [s for s in data["shots"] if s.get("source_kind") == "stock"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    if not shots:
        raise StockError("没有 source_kind=stock 的镜头")

    gen_dir = project_dir / "gen"
    results = []
    for shot in shots:
        sid = shot["id"]
        target = project_dir / "clips" / f"{sid}.mp4"
        if target.is_file() and not force:
            results.append({"shot_id": sid, "skipped": f"clips/{sid}.mp4 已存在"})
            continue
        try:
            spec = shot.get("stock") or {}
            query = (spec.get("query") or "").strip()
            if not query:
                raise StockError(
                    f"{sid}: 缺 stock.query。混剪镜头必须写检索关键词，"
                    "不然不知道要找什么。"
                )
            need = float(spec.get("min_duration") or shot["duration"])

            candidates = search(cfg, query)
            chosen = pick(candidates, need, ch)
            got = download(cfg, chosen, gen_dir)

            src = Path(got["path"])
            real_w, real_h = got.get("width"), got.get("height")
            real_dur = float(got.get("duration") or 0)
            if not src.is_file():
                raise StockError(f"{sid}: 下载返回的路径不存在 {src}")

            normalize(media, src, target, float(shot["duration"]), cw, ch, cfps,
                      bool(cfg.get("stock.keep_audio", False)))
            info = media.probe(target)

            row = {
                "shot_id": sid,
                "query": query,
                "candidates": len(candidates),
                "picked": {"provider": got.get("provider"), "id": got.get("id"),
                           "title": chosen.get("title")},
                "source_resolution": [real_w, real_h],
                "source_duration": round(real_dur, 3),
                "out": f"clips/{sid}.mp4",
                "out_resolution": [info["width"], info["height"]],
                "duration_sec": round(info["duration_sec"], 3),
                "has_audio": info["has_audio"],
                "bytes": target.stat().st_size,
                "license": chosen.get("license"),
                "page_url": chosen.get("page_url"),
            }
            if real_dur and real_dur + 0.05 < float(shot["duration"]):
                row["warning"] = (
                    f"素材实际只有 {real_dur:.2f}s，短于镜头需要的 "
                    f"{shot['duration']:.2f}s —— 会被截短或末帧定格"
                )
            results.append(row)
        except Exception as exc:      # noqa: BLE001 —— 单条失败不该让整批停摆
            results.append({"shot_id": sid, "error": str(exc)})

    report = {"canvas": [cw, ch, cfps], "results": results}
    artifacts.write_json(project_dir / "stock.json", report)
    return report


def summarize(report: dict) -> tuple[list[str], list[str]]:
    ok, bad = [], []
    for r in report.get("results", []):
        sid = r.get("shot_id")
        if r.get("error"):
            bad.append(f"  {sid}: ✗ {r['error']}")
        elif r.get("skipped"):
            ok.append(f"  {sid}: 跳过 —— {r['skipped']}")
        else:
            p = r.get("picked") or {}
            ok.append(f"  {sid}: 「{r['query']}」→ {p.get('provider')}/{p.get('id')}"
                      f"  {r['source_resolution'][0]}x{r['source_resolution'][1]}"
                      f" {r['source_duration']}s"
                      f" → {r['out_resolution'][0]}x{r['out_resolution'][1]}"
                      f" {r['duration_sec']}s"
                      f"  {r['bytes'] / 1024 / 1024:.1f}MB"
                      f"  音轨={'有' if r['has_audio'] else '无'}")
            if r.get("warning"):
                ok.append(f"      ! {r['warning']}")
    return ok, bad
