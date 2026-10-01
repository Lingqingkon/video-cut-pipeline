"""检测画面里有没有模型烧进去的文字。

为什么需要
----------
MiniMax-H3 会把台词渲染成画面内文字（实测 7 条里 2 条中招），而我们自己在剪映里
也会加字幕 —— 成片底部会出现两套字。

**为什么不做裁边**：实测烧字位置不固定（一条在 70% 高度、另一条在 85%），
要裁掉就得裁画面 40% 高度，不可接受。所以只能检测出来重生成。

原理
----
文字 = 小尺寸、高对比、**密集的竖笔画**。在一条水平带里表现为"连续多行、
每行的水平梯度都很多"。而西装、衬衫、窗户、皮肤是大面积平滑区，梯度集中在
少数几条长边上，不会形成密集带。

所以：按行统计水平梯度超阈值的像素占比，再看有没有一条**连续多行都高**的带。

用 ffmpeg 输出灰度原始像素 + numpy 算，不引入新依赖（numpy 是 faster-whisper 的依赖，已在）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import artifacts


class TextCheckError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


def _gray_region(ffmpeg: str, clip: Path, t: float, width: int, height: int,
                 y0: float, y1: float) -> np.ndarray | None:
    """取某一时刻画面 y0~y1 区域的灰度图，返回 (h, w) uint8。

    **按实际拿到的行数 reshape，不能按请求的行数。** ffmpeg 的 crop 会把高度
    按偶数对齐（yuv420p 的色度下采样要求），请求 39 行只会给 38 行。
    老写法是 `if len(raw) < width*h: return None` —— 请求奇数行时**永远**返回 None，
    而上层只会看到"什么都没读到"，分不出"读不到"和"真的没有内容"。
    实测踩到过：去烧字用 626..666（39 行，奇数）算 mask 时白像素数恒为 0。
    """
    top = int(height * y0)
    h = max(1, int(height * (y1 - y0)))
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(clip),
           "-frames:v", "1", "-vf", f"crop={width}:{h}:0:{top}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    raw = proc.stdout
    rows = len(raw) // width
    # 允许少 1 行（偶数对齐），再少就是真没读到
    if rows < max(1, h - 1):
        return None
    return np.frombuffer(raw[: width * rows], dtype=np.uint8).reshape(rows, width)


def row_edge_profile(gray: np.ndarray, thresh: int = 36) -> np.ndarray:
    """每一行里"水平梯度超阈值"的像素占比。文字带会明显高出周围。"""
    g = gray.astype(np.int16)
    dx = np.abs(np.diff(g, axis=1))
    return (dx > thresh).mean(axis=1)


def band_score(profile: np.ndarray, band: int) -> tuple[float, int]:
    """滑动窗口找最"密"的一条带，返回 (该带的平均占比, 带顶部的行号)。

    用连续多行的平均而不是单行最大值：单行最大会被一条长边骗过去，
    文字是**一片**，不是一条线。
    """
    if len(profile) < band:
        return (float(profile.mean()) if len(profile) else 0.0, 0)
    cum = np.concatenate([[0.0], np.cumsum(profile)])
    sums = cum[band:] - cum[:-band]
    means = sums / band
    i = int(np.argmax(means))
    return float(means[i]), i


def measure(ffmpeg: str, clip: Path, probe: dict,
            times: list[float] | None = None,
            y0: float = 0.50, y1: float = 1.0,
            band_rows: int = 28, thresh: int = 36) -> dict:
    """对一个片段多抽几帧，返回最可疑那一帧的指标。

    多帧取"最可疑"而不是平均：烧字不一定全程都在（可能只在说某句话时出现），
    平均会把它稀释掉。
    """
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    dur = float(probe.get("duration_sec") or 0)
    if not width or not height:
        raise TextCheckError(f"{clip.name}: 探不到分辨率")

    if times is None:
        # 避开首尾各 10%，均匀取 5 帧
        times = [dur * f for f in (0.12, 0.3, 0.5, 0.7, 0.88)] if dur > 0.5 else [dur * 0.5]

    region_h = max(1, int(height * (y1 - y0)))
    band_rows = max(6, min(band_rows, region_h // 2))

    best = {"score": 0.0, "row": 0, "t": None, "profile": None}
    for t in times:
        gray = _gray_region(ffmpeg, clip, t, width, height, y0, y1)
        if gray is None:
            continue
        profile = row_edge_profile(gray, thresh)
        score, row = band_score(profile, band_rows)
        if score > best["score"]:
            best = {"score": score, "row": row, "t": round(t, 3), "profile": profile}

    if best["t"] is None:
        raise TextCheckError(f"{clip.name}: 一帧都取不到")

    # 相对指标：可疑带比该区域中位数密多少倍。绝对阈值会受画面风格影响，
    # 相对值更能跨镜头比较。
    med = float(np.median(best["profile"]))
    ratio = best["score"] / med if med > 1e-6 else float("inf")
    band_top_pct = y0 + (best["row"] / region_h) * (y1 - y0)
    # 带子的**像素行范围**。去烧字要把它当 mask 交给 VSR；只给百分比的话
    # height*百分比 会有舍入误差，差几行就会漏掉字的上下边缘。
    band_top_px = int(height * y0) + int(best["row"])
    band_bottom_px = min(int(height), band_top_px + band_rows)

    best.pop("profile", None)
    return {
        "clip": clip.name,
        "frames_checked": len(times),
        "score": round(best["score"], 4),
        "median_row_density": round(med, 4),
        "ratio": round(ratio, 2) if ratio != float("inf") else None,
        "band_top_pct": round(band_top_pct, 3),
        "band_top_px": band_top_px,
        "band_bottom_px": band_bottom_px,
        "at_second": best["t"],
    }


# 在 10 条标注样本（4 正 6 负）上定标：正样本 score 落在 0.186~0.216，
# 负样本除 0.1244（一条衬衫领口造成的）外都在 0.026 以下。
# 阈值取 0.14：零漏报零误报，且离两簇都有余量。
# **宁可误报不可漏报** —— 误报代价是多跑一次生成（~90 秒），
# 漏报代价是双份字幕流到客户手里。
DEFAULT_THRESHOLD = 0.14


def suspect(metric: dict, threshold: float = DEFAULT_THRESHOLD) -> bool:
    return float(metric.get("score") or 0.0) >= threshold


def check_clip(cfg, media, clip: Path, threshold: float | None = None) -> dict:
    thr = float(threshold if threshold is not None
                else cfg.get("textcheck.threshold", DEFAULT_THRESHOLD))
    m = measure(cfg.require("ffmpeg.ffmpeg"), clip, media.probe(clip))
    m["threshold"] = thr
    m["suspect"] = suspect(m, thr)
    m["clip_path"] = str(clip)
    return m


def scan(cfg, project_dir: Path, only=None) -> dict:
    """扫一遍项目里的片段，报哪些可能带烧字。"""
    from .media import Media

    project_dir = Path(project_dir)
    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
    data = artifacts.load_shots(project_dir)

    shots = data["shots"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]

    results = []
    for shot in shots:
        clip = artifacts.resolved_clip(project_dir, shot, cfg)
        if not clip.is_file():
            results.append({"shot_id": shot["id"], "error": f"找不到 {clip}"})
            continue
        try:
            m = check_clip(cfg, media, clip)
            m["shot_id"] = shot["id"]
            results.append(m)
        except Exception as exc:      # noqa: BLE001 —— 单条失败不拖累其余
            results.append({"shot_id": shot["id"], "error": str(exc)})

    report = {
        "threshold": float(cfg.get("textcheck.threshold", DEFAULT_THRESHOLD)),
        "suspect_count": sum(1 for r in results if r.get("suspect")),
        "checked": len(results),
        "results": results,
    }
    artifacts.write_json(project_dir / "textcheck.json", report)
    return report


def summarize(report: dict) -> tuple[list[str], list[str]]:
    """返回 (正常行, 可疑行)。"""
    ok, bad = [], []
    for r in report.get("results", []):
        sid = r.get("shot_id")
        if r.get("error"):
            bad.append(f"  {sid}: ✗ {r['error']}")
        elif r.get("suspect"):
            bad.append(f"  {sid}: ⚠ score={r['score']}（阈值 {r['threshold']}）"
                       f"  可疑带在 {r['band_top_pct']:.0%} 高度")
        else:
            ok.append(f"  {sid}: score={r['score']}  干净")
    return ok, bad
