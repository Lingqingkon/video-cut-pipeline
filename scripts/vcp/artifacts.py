"""产物读写与校验。

契约见 ../references/artifacts.md。这里是唯一的读写入口，
任何环节都不许自己拼路径或自己造字段名。
"""

from __future__ import annotations

import json
from pathlib import Path

# 项目目录布局。改这里等于改契约，要同步改 references/artifacts.md。
SUBDIRS = ("gen", "clips", "clips_sr", "clips_clean", "audio", "assets",
           "preview", "align", "incoming")

SHOT_REQUIRED = ("id", "source_kind", "shot_kind", "duration", "file")
SOURCE_KINDS = ("aigc", "stock", "footage")
SHOT_KINDS = ("talk", "broll", "product")


class ArtifactError(Exception):
    pass


def ensure_layout(project_dir: Path | str) -> Path:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (project_dir / sub).mkdir(exist_ok=True)
    return project_dir


def write_json(path: Path | str, obj) -> Path:
    """统一用 UTF-8 + 不转义中文 + 缩进 1 空格，跟既有 manifest 的风格保持一致。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_json(path: Path | str):
    path = Path(path)
    if not path.is_file():
        raise ArtifactError(f"文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ── shots.json ────────────────────────────────────────────────────────────

def shots_path(project_dir) -> Path:
    return Path(project_dir) / "shots.json"


def validate_shots(data: dict) -> list[str]:
    """返回问题清单。空列表 = 通过。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["shots.json 顶层必须是对象"]

    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        return ["shots.json 缺少非空的 shots 数组"]

    seen: set[str] = set()
    for idx, shot in enumerate(shots):
        where = f"shots[{idx}]"
        if not isinstance(shot, dict):
            problems.append(f"{where} 不是对象")
            continue
        sid = shot.get("id") or f"<无 id 的第 {idx} 条>"
        where = f"shots[{idx}] ({sid})"

        for field in SHOT_REQUIRED:
            if shot.get(field) in (None, ""):
                problems.append(f"{where} 缺必填字段 {field}")

        if sid in seen:
            problems.append(f"{where} id 重复")
        seen.add(sid)

        kind = shot.get("source_kind")
        if kind and kind not in SOURCE_KINDS:
            problems.append(f"{where} source_kind={kind!r} 不合法，只能是 {SOURCE_KINDS}")

        skind = shot.get("shot_kind")
        if skind and skind not in SHOT_KINDS:
            problems.append(f"{where} shot_kind={skind!r} 不合法，只能是 {SHOT_KINDS}")

        dur = shot.get("duration")
        if dur is not None and (not isinstance(dur, (int, float)) or dur <= 0):
            problems.append(f"{where} duration 必须是正数，实际 {dur!r}")

        # AIGC 的 talk 镜头必须有 text —— 它是第 3 段听检的比对基准
        if kind == "aigc" and skind == "talk" and not shot.get("text"):
            problems.append(f"{where} 是 aigc + talk，必须填 text（ASR 听检要拿它做基准）")

        if shot.get("keyphrase") and not shot.get("text"):
            problems.append(f"{where} 标了 keyphrase 但没有 text，无法定位重点句")

        # 混剪镜头必须有检索关键词，不然第 2a 段不知道要找什么
        if kind == "stock" and not ((shot.get("stock") or {}).get("query") or "").strip():
            problems.append(f"{where} 是 stock，必须填 stock.query（检索关键词）")

        # AIGC 镜头必须有 prompt
        if kind == "aigc" and not (shot.get("prompt") or "").strip():
            problems.append(f"{where} 是 aigc，必须填 prompt")

    return problems


def load_shots(project_dir) -> dict:
    data = read_json(shots_path(project_dir))
    problems = validate_shots(data)
    if problems:
        raise ArtifactError(
            "shots.json 校验不通过：\n  - " + "\n  - ".join(problems)
        )
    return data


def patch_shot(project_dir, shot_id: str, fields: dict) -> dict:
    """按字段回写单个镜头，其它内容原样写回（保留中文不转义、缩进风格一致）。

    给"实测值覆盖计划值"用：口播素材入库按实测时长改正、生成按实测帧数改正。
    后两者都是**视频说了算**，镜头表要反过来对齐它。

    `fields` 里的 dict（如 `gen`）是**合并**而不是整体覆盖 —— 只改点名的那几个键，
    不动同一个 dict 里的其它字段。
    """
    path = shots_path(project_dir)
    data = read_json(path)
    for shot in data.get("shots") or []:
        if shot.get("id") == shot_id:
            for key, value in fields.items():
                if isinstance(value, dict) and isinstance(shot.get(key), dict):
                    shot[key] = {**shot[key], **value}
                else:
                    shot[key] = value
            write_json(path, data)
            return shot
    raise ArtifactError(f"shots.json 里找不到镜头 {shot_id}")


def clip_file(project_dir, shot: dict) -> Path:
    """取镜头对应的**原始**片段绝对路径。允许 shots.json 里写相对路径。"""
    raw = Path(shot["file"])
    return raw if raw.is_absolute() else Path(project_dir) / raw


def resolved_clip(project_dir, shot: dict, cfg=None) -> Path:
    """决定这个镜头最终用哪个文件。

    优先级：
    1. `upscale.enabled` 且 `clips_sr/<id>.mp4` 存在 → 超分产物
    2. `desub.enabled`   且 `clips_clean/<id>.mp4` 存在 → 去过烧字的产物
    3. `clips/` 里的原始片段

    做成开关而不是手工换文件，是为了避免"半套"状态：一半镜头处理过了、
    一半没有，而 manifest 里分不出来。第 4 段生成 manifest 时必须走这个函数，
    不要直接读 shot["file"]。

    **超分排在去字前面**：超分是拿 clips/ 做的，产物在 clips_sr/。两个都开的时候
    顺序应该先去字再超分（超分跑在 clips_clean 上），否则超分会把烧字一起放大 ——
    这条目前没实跑过，真要同时开先在一条镜头上验一遍。
    """
    if cfg is not None:
        if cfg.get("upscale.enabled", False):
            sr = Path(project_dir) / "clips_sr" / f"{shot['id']}.mp4"
            if sr.is_file():
                return sr
        if cfg.get("desub.enabled", False):
            clean = Path(project_dir) / "clips_clean" / f"{shot['id']}.mp4"
            if clean.is_file():
                return clean
    return clip_file(project_dir, shot)


# ── align.json ────────────────────────────────────────────────────────────

def align_path(project_dir, shot_id: str) -> Path:
    return Path(project_dir) / "align" / f"{shot_id}.json"


def write_align(project_dir, shot_id: str, payload: dict) -> Path:
    return write_json(align_path(project_dir, shot_id), payload)


def load_align_all(project_dir) -> dict[str, dict]:
    """读回所有已对齐的镜头，key 是 shot id。缺哪个就在后面的校验里报出来。"""
    out: dict[str, dict] = {}
    align_dir = Path(project_dir) / "align"
    if not align_dir.is_dir():
        return out
    for path in sorted(align_dir.glob("*.json")):
        out[path.stem] = read_json(path)
    return out
