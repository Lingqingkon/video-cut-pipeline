"""第 2c' 段：去烧字 —— 把模型烧进画面的台词从像素上抹掉。

为什么需要单独一段
------------------
MiniMax-H3 会把台词渲染成画面内文字（实测 7 条里 2 条中招，约 25%），而我们在剪映里
还要加字幕 —— 成片底部会出现两套字，**而烧进去的那套删不掉（它在像素里）**。

两条路已经排除，别再走：
- **提示词压制无效**：`generation.prompt_suffix` 明确写了"不出现任何文字"，模型照样出
- **裁边是断的**：烧字位置不固定（实测 73% / 80% 高度都有），要裁掉得裁 40% 画面

所以走"生成后用 AI 补帧"这条路。

原理
----
1. 定位：我们自己已经能做 —— `vcp textcheck` 给出分数和"可疑带"的像素行范围
2. 挖掉：把带子上下各留几像素当 mask，交给 VSR 用 STTN 把这块重绘
3. VSR **只重绘被 mask 的像素**，其余画面一个字节不动

三条实测结论（别重走）
----------------------
- **必须用 `sttn-det`。** `sttn-auto` 抹不掉字：那个模型的输入是 3 通道、**没有 mask**，
  指望它自己"学会"擦字。实测同一块区域文字笔画像素 1589 → 1589，等于没做。
  `sttn-det` 走的是 `feats*(1-masks)`，是真把 mask 挖掉再补，实测 1589 → 0。
- **只重绘检测到文字的帧**。sttn-det 会先自己 OCR 一遍定帧范围，实测 c3 只有第 27–65
  帧有字，其余 165 帧原样保留。
- **VSR 自己会把原音轨合回去**（`-acodec copy`），音轨是逐字节复制的，
  所以时间基准不变 —— 这一点很重要，`align` 和 `keyphrase` 都靠它。

产物与开关
----------
写 `clips_clean/<id>.mp4`，**绝不覆盖 `clips/`**。选哪个文件由
`artifacts.resolved_clip()` 按 `desub.enabled` 决定 —— 和超分那套同一个机制，
避免"一半镜头去了字、一半没有"而 manifest 里看不出来的半套状态。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import artifacts, textcheck
from .comfyenv import resolve_password
from .remote import Remote


class DesubError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


def target(project_dir, shot_id: str) -> Path:
    """去字产物的落点。和 clips_sr/ 同一个思路：另起目录，不覆盖原始片段。"""
    return Path(project_dir) / "clips_clean" / f"{shot_id}.mp4"


def _remote(cfg) -> Remote:
    return Remote(
        host=cfg.require("remote.host"),
        port=int(cfg.get("remote.port", 22)),
        user=cfg.get("remote.user", "root"),
        password=resolve_password(cfg),
        key_filename=cfg.get("remote.key_file") or None,
    ).connect()


def band_coords(metric: dict, pad: int, width: int,
                height: int | None = None) -> tuple[int, int, int, int]:
    """把 textcheck 的带子行范围转成 VSR 的 -c 参数 (YMIN YMAX XMIN XMAX)。

    宽度给满：文字是横向排布的，我们只靠行范围定位，不猜它的左右边界 ——
    猜窄了会留下半个字的边，猜宽了也只是多补一点背景，代价不对等。

    `height` 用来把 y1 夹在画面内：带子本来就在底部时 +pad 会越界，
    越界的坐标传给 VSR 是没意义的（它按 crop 处理，越界部分会被忽略或报错）。
    """
    y0 = max(0, int(metric.get("band_top_px") or 0) - pad)
    y1 = int(metric.get("band_bottom_px") or 0) + pad
    if height:
        y1 = min(int(height), y1)
    return y0, y1, 0, int(width)


def stroke_count(ffmpeg: str, clip: Path, probe: dict,
                 y0: int, y1: int, times: list[float]) -> int:
    """带子里"接近白色"的像素数（取多帧最大值）。

    为什么不能只看 score：score 是"密集竖笔画"的统计量，皮带、衬衫边缘、窗棂
    也会贡献。实测把字抹掉之后 score 只从 0.1473 掉到 0.1272 —— 离阈值 0.14 只差 9%，
    看着像"还是可疑"。而同一块区域的白像素是 1589 → 0。烧字是白字黑边，
    这个指标才直接反映"字还在不在"。
    """
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    if not width or not height or y1 <= y0:
        return -1
    y0f, y1f = y0 / height, min(1.0, y1 / height)
    best = 0
    read_any = False
    for t in times:
        gray = textcheck._gray_region(ffmpeg, clip, t, width, height, y0f, y1f)
        if gray is None:
            continue
        read_any = True
        best = max(best, int((gray > 200).sum()))
    # **"读不到帧"必须和"没有白像素"分开。** 两者都返回 0 的话，复检会把
    # "根本没读到画面"当成"字已抹掉"，直接放过坏产物 —— 这个坑踩过一次
    # （当时是 crop 高度被 ffmpeg 按偶数对齐，导致每一帧都读不到）。
    return best if read_any else -1


def process_one(cfg, media, project_dir: Path, shot: dict, remote: Remote,
                force: bool = False) -> dict:
    sid = shot["id"]
    src = artifacts.clip_file(project_dir, shot)
    dst = target(project_dir, sid)

    if not src.is_file():
        raise DesubError(f"{sid}: 找不到原始片段 {src}")
    if dst.is_file() and not force:
        return {"shot_id": sid, "skipped": f"clips_clean/{sid}.mp4 已存在（要重做加 --force）"}

    ffmpeg = cfg.require("ffmpeg.ffmpeg")
    threshold = float(cfg.get("textcheck.threshold", textcheck.DEFAULT_THRESHOLD))
    info = media.probe(src)
    metric = textcheck.check_clip(cfg, media, src, threshold=threshold)
    pad = int(cfg.get("desub.coords_pad", 6))
    y0, y1, x0, x1 = band_coords(metric, pad, info["width"], info["height"])
    times = [float(info["duration_sec"]) * f for f in (0.12, 0.3, 0.5, 0.7, 0.88)]
    strokes_before = stroke_count(ffmpeg, src, info, y0, y1, times)

    # 定位方式（`desub.locate`）：
    #   bottom —— **只让 OCR 在下半部分找字**（传一个覆盖下半屏的搜索区，但精确框仍由
    #             OCR 决定）。烧字是字幕，只出现在下半部分 —— 我们自己的 textcheck
    #             本来就只看 y=0.50~1.0。这是默认。
    #   auto   —— 不给 -c，OCR 扫整帧。**实测会误伤**：VSR 的 PaddleOCR 把说书人的
    #             眼睛/鼻梁认成了文字，STTN 把那 62 行挖掉重绘，脸被抹平。
    #             复检还拦不住 —— 脸被抹平后那条带子更"干净"，score 只会更低。
    #   coords —— 把 textcheck 量出来的带子当 mask 传给 VSR。干净镜头的那条带子
    #             也会被重绘（那条带子只是"边缘最密"，不一定有字）。
    locate = str(cfg.get("desub.locate", "bottom")).lower()
    if locate not in ("bottom", "auto", "coords"):
        raise DesubError(f"desub.locate 只能是 bottom / auto / coords，实际是 {locate!r}")

    if locate == "coords" and not metric["suspect"] and bool(cfg.get("desub.only_suspect", True)):
        return {
            "shot_id": sid,
            "skipped": f"textcheck 没判为可疑（score {metric['score']} < {threshold}）",
            "textcheck_before": metric["score"],
            "locate": locate,
        }

    root = str(cfg.require("desub.remote_root")).rstrip("/")
    rpy = str(cfg.get("desub.python") or f"{root}/venv/bin/python")
    rrepo = str(cfg.get("desub.repo") or f"{root}/video-subtitle-remover")
    mode = str(cfg.get("desub.mode", "sttn-det"))
    rwork = f"{root}/desub_work"
    r_in, r_out = f"{rwork}/{sid}.mp4", f"{rwork}/{sid}_clean.mp4"

    remote.run(f"mkdir -p {rwork}", timeout=60)
    # 先删掉上一轮的产物：不删的话"这次其实失败了"会拿到旧文件，
    # 复检也过了，于是一条没被处理的镜头被当成处理过的。
    remote.run(f"rm -f {r_out}", timeout=60)
    remote.put(str(src), r_in)

    cmd = f"cd {rrepo} && {rpy} backend/main.py -i {r_in} -o {r_out}"
    if locate == "bottom":
        # 搜索区给"下半屏"，但**精确框仍由 OCR 决定** —— sttn-det 走的是
        # video_inpaint()，mask 来自检测器的输出，不是我们给的整块区域。
        # 所以这里只是告诉它"往哪看"，不会把整个下半屏都重绘。
        coords_used = [int(info["height"]) // 2, int(info["height"]), 0, int(info["width"])]
    elif locate == "coords":
        coords_used = [y0, y1, x0, x1]
    else:
        coords_used = None
    if coords_used:
        cmd += " -c " + " ".join(str(int(v)) for v in coords_used)
    cmd += f" --inpaint-mode {mode}"

    started = time.time()
    log = remote.run(cmd, timeout=int(cfg.get("desub.timeout", 1800)))
    elapsed = time.time() - started

    if remote.stat_size(r_out) <= 0:
        # auto 模式下"整帧一帧字都没找到"是本条镜头很正常的结果 —— VSR 在
        # main.py:264 抛 Exception(tr['Main']['NoSubtitleDetected'])，不产出文件。
        # **这不是失败**：这条镜头本来就干净，一个像素都不该动。
        # 两个串都匹配：一个来自 en.ini 的实际文案（`No subtitles detected. Check file:`），
        # 一个来自 traceback 会回显的那行源码（`tr['Main']['NoSubtitleDetected']`）。
        # 只匹一个的话，换个 Python/traceback 格式就会把所有干净镜头判成失败。
        if locate in ("auto", "bottom") and ("NoSubtitleDetected" in log
                                             or "No subtitles detected" in log):
            return {
                "shot_id": sid,
                "skipped": "OCR 在整帧里没找到任何字幕 —— 判定为干净，未改动任何像素",
                "textcheck_before": metric["score"],
                "locate": locate,
                "seconds": round(elapsed, 1),
            }
        raise DesubError(f"{sid}: VSR 没产出文件。日志尾部：\n{log[-800:]}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    remote.get(r_out, str(dst))

    out_info = media.probe(dst)
    result = {
        "shot_id": sid,
        "source": f"clips/{sid}.mp4",
        "out": f"clips_clean/{sid}.mp4",
        "mode": mode,
        "locate": locate,
        "coords": coords_used,
        "textcheck_before": metric["score"],
        "band_top_pct": metric["band_top_pct"],
        "strokes_before": strokes_before,
        "resolution_before": [info["width"], info["height"]],
        "resolution_after": [out_info["width"], out_info["height"]],
        "frames_before": round(info["duration_sec"] * (info["fps"] or 0)),
        "frames_after": round(out_info["duration_sec"] * (out_info["fps"] or 0)),
        "has_audio_after": out_info["has_audio"],
        "seconds": round(elapsed, 1),
    }

    # ── 硬断言 ────────────────────────────────────────────────────────────
    # 这三条任何一条不过，产物都不能用：分辨率变了=画布不匹配；
    # 帧率变了=时间轴整体漂；比原片短=排轨会出现黑尾。
    if result["resolution_after"] != result["resolution_before"]:
        result["error"] = (f"分辨率被改了：{result['resolution_before']} -> "
                           f"{result['resolution_after']}")
    elif abs(float(out_info["fps"] or 0) - float(info["fps"] or 0)) > 0.01:
        result["error"] = f"帧率被改了：{info['fps']} -> {out_info['fps']}"
    elif result["frames_after"] < result["frames_before"]:
        result["error"] = (f"产物比原片短：{result['frames_before']} -> "
                           f"{result['frames_after']} 帧，排轨会出现黑尾")
    elif not out_info["has_audio"]:
        result["error"] = "产物没有音轨（VSR 合音轨那步失败了），时间基准会废掉"
    elif not bool(cfg.get("desub.recheck", True)):
        pass
    else:
        m2 = textcheck.check_clip(cfg, media, dst, threshold=threshold)
        result["textcheck_after"] = m2["score"]
        result["strokes_after"] = stroke_count(
            ffmpeg, dst, out_info,
            int(m2.get("band_top_px") or y0), int(m2.get("band_bottom_px") or y1), times)
        # **判定用 score，不用白像素。** score 是标定过的：干净画面 0.012~0.021，
        # 烧字 0.147~0.148，阈值 0.14 落在两簇中间。
        # 白像素数只当诊断 —— 它会被带子里本来就亮的东西带偏：实测某项目 c1
        # 那条带子里是一只手端着白纸杯，score 只有 0.1355（不算烧字）却有 1278 个
        # 白像素，一个字都没有。拿它当硬判定会把好产物判死。
        if float(m2["score"]) < threshold:
            result["verdict"] = "字已抹掉"
            if result["strokes_after"] < 0:
                result["verdict"] += "（白像素没量到，建议肉眼看一眼）"
            elif strokes_before > 0 and result["strokes_after"] >= strokes_before:
                result["verdict"] += "（白像素没降，可能是带子里本来就有亮物）"
        else:
            result["error"] = (
                f"复检 score 还是 {m2['score']}（阈值 {threshold}）—— 字没抹干净。"
                f"白像素 {strokes_before} -> {result['strokes_after']}。"
                f"把 desub.coords_pad 调大（现在 {pad}）加 --force 再跑一次，"
                f"或改用 `vcp generate --force` 换 seed 重生成"
            )

    if result.get("error") and bool(cfg.get("desub.drop_on_fail", True)):
        # 复检不过就别留着 —— 留着会被 resolved_clip 选中，等于把坏产物当好的用
        try:
            dst.unlink()
        except OSError:
            pass
        result["out"] = None

    return result


def run(cfg, project_dir: Path, only=None, force: bool = False) -> dict:
    from .media import Media

    project_dir = Path(project_dir)
    artifacts.ensure_layout(project_dir)
    data = artifacts.load_shots(project_dir)
    shots = data["shots"]
    if only:
        wanted = set(only)
        shots = [s for s in shots if s["id"] in wanted]
    if not shots:
        raise DesubError("没有镜头要处理")

    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
    results: list[dict] = []
    with _remote(cfg) as remote:
        for shot in shots:
            try:
                results.append(process_one(cfg, media, project_dir, shot, remote, force=force))
            except Exception as exc:      # noqa: BLE001 —— 单条失败不拖累其余
                results.append({"shot_id": shot["id"], "error": str(exc)})

    report = {
        "locate": str(cfg.get("desub.locate", "bottom")),
        "mode": str(cfg.get("desub.mode", "sttn-det")),
        "only_suspect": bool(cfg.get("desub.only_suspect", True)),
        "coords_pad": int(cfg.get("desub.coords_pad", 6)),
        "results": results,
    }
    artifacts.write_json(project_dir / "desub.json", report)
    return report


def summarize(report: dict) -> tuple[list[str], list[str]]:
    """返回 (正常行, 需要处理的行)。

    复检不过的进第二个列表 —— 产物已被删掉，等于这条镜头还是烧着字，
    不能让它静默流到成片里。
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
        ok.append(
            f"  {sid}: score {r['textcheck_before']} -> {r.get('textcheck_after')}"
            f"   白像素 {r['strokes_before']} -> {r.get('strokes_after')}"
            f"   {r['frames_before']}->{r['frames_after']} 帧"
            f"   坐标 {' '.join(str(c) for c in r['coords'])}"
            f"   {r['seconds']}s   {r.get('verdict') or ''}"
        )
    return ok, bad
