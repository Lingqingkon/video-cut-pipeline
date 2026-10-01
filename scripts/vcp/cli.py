"""vcp 命令行入口。

  python -m vcp init        <project-id> [--work-root DIR] [--task-type T]
  python -m vcp import-spec <project-dir> --spec FILE [--name NAME]
  python -m vcp check       <project-dir>
  python -m vcp generate    <project-dir> [--only ID ...] [--yes]
  python -m vcp align       <project-dir> [--only ID ...]
  python -m vcp upscale     <project-dir> [--only ID ...]
  python -m vcp manifest    <project-dir> [--draft-name NAME]

退出码：0 正常 / 1 出错 / 2 听检发现不匹配（刻意用非零，避免被忽略过去）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import align as align_stage
from . import (artifacts, config, generate as generate_stage, legacyspec,
               manifest as manifest_stage, upscale as upscale_stage)
from .media import Media

TEMPLATE = Path(__file__).resolve().parents[2] / "config.example.yaml"

OK, ERR, MISMATCH = 0, 1, 2


def _cfg(project_dir: Path):
    return config.load(project_dir)


def cmd_init(args) -> int:
    base = Path(args.work_root).expanduser() if args.work_root else None
    if base is None:
        cfg = config.load(None)
        base = cfg.get("paths.work_root")
        if not base:
            print("× 没有 work_root。用 --work-root 指定，或在用户级配置里设 paths.work_root。", file=sys.stderr)
            return ERR
    project_dir = Path(base) / args.project_id

    existed = project_dir.exists()
    artifacts.ensure_layout(project_dir)

    target = project_dir / "config.yaml"
    if target.exists() and not args.force:
        print(f"· {target} 已存在，保留不动（要覆盖加 --force）")
    else:
        if not TEMPLATE.is_file():
            print(f"× 找不到配置模板：{TEMPLATE}", file=sys.stderr)
            return ERR
        shutil.copyfile(TEMPLATE, target)
        print(f"+ {target}")

    if args.task_type:
        _patch_task_type_hint(target, args.task_type)

    print(f"{'·' if existed else '+'} 项目目录 {project_dir}")
    for sub in artifacts.SUBDIRS:
        print(f"+ {project_dir / sub}")
    print("\n下一步：把文稿放进 script.md，然后让模型按 SKILL.md 第 1 段产出 shots.json。")
    return OK


def _patch_task_type_hint(config_path: Path, task_type: str) -> None:
    """只在模板顶部加一行注释标明任务型，不动其它内容。"""
    text = config_path.read_text(encoding="utf-8")
    text = f"# task_type: {task_type}\n" + text
    config_path.write_text(text, encoding="utf-8")


def cmd_import_spec(args) -> int:
    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
    spec_path = Path(args.spec)

    data = legacyspec.import_spec(
        project_dir, spec_path, media,
        project_name=args.name or project_dir.name,
        task_type=args.task_type,
    )

    problems = artifacts.validate_shots(data)
    if problems:
        print("× 迁移结果没通过校验：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return ERR

    out = artifacts.write_json(artifacts.shots_path(project_dir), data)
    print(f"+ {out}")
    print(f"  镜头 {len(data['shots'])} 条（来自 {spec_path}）")
    for w in data.get("_warnings") or []:
        print(f"  ! {w}")
    return OK


def cmd_check(args) -> int:
    project_dir = Path(args.project_dir)
    path = artifacts.shots_path(project_dir)
    if not path.is_file():
        print(f"× 没有 {path}", file=sys.stderr)
        return ERR

    data = artifacts.read_json(path)
    problems = artifacts.validate_shots(data)
    shots = data.get("shots") or []

    by_kind: dict[str, int] = {}
    for s in shots:
        by_kind[s.get("source_kind") or "?"] = by_kind.get(s.get("source_kind") or "?", 0) + 1

    print(f"shots.json：{len(shots)} 条镜头")
    print(f"  素材分支 {by_kind}")
    print(f"  重点句 {sum(1 for s in shots if s.get('keyphrase'))} 条")
    print(f"  有台词 {sum(1 for s in shots if s.get('text'))} 条")

    if problems:
        print(f"\n× 校验不通过，{len(problems)} 个问题：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return ERR

    print("\n√ 校验通过")
    return OK


def cmd_align(args) -> int:
    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    result = align_stage.run(cfg, project_dir, only=args.only)
    ok, bad = align_stage.summarize(result["results"])

    print(f"第 3 段：对齐与听检（{len(result['results'])} 个镜头）\n")
    if ok:
        print("通过：")
        for line in ok:
            print(line)
    if bad:
        print("\n需要人确认（ASR 识别出的实际语音与给定台词不一致）：")
        for line in bad:
            print(line)
        print(
            "\n这几条不许静默继续。要么生成侧确实念错了、要重生成，"
            "\n要么台词写错了、要改 shots.json 的 text。确认之后再往下走。"
        )
        return MISMATCH

    print("\n√ 全部对齐完成，align/ 下已写出逐字时间戳")
    return OK


def cmd_upscale(args) -> int:
    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    report = upscale_stage.run(cfg, project_dir, only=args.only)
    ok, bad = upscale_stage.summarize(report)

    info = report.get("backend") or {}
    results = report.get("results") or []
    print(f"第 2.5 段：超分（{len(results)} 个镜头）")
    print(f"  后端 {info.get('mode')}  {info.get('base')}")
    print(f"  模型 {report.get('model')}   目标 {report.get('target')}\n")

    for line in ok:
        print(line)
    if bad:
        print("\n失败：")
        for line in bad:
            print(line)
        return ERR

    print("\n√ 超分完成。产物在 clips_sr/，报告在 upscale.json")
    print("  原始片段保持在 clips/ 未动 —— 超分是可重跑的后期步骤，源头要留着。")
    return OK


def cmd_generate(args) -> int:
    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)

    plan = generate_stage.plan(cfg, project_dir, only=args.only)
    print("第 2b 段：AIGC 生成")
    print(f"  后端 comfyui.mode = {cfg.get('comfyui.mode')}")
    print()

    todo = []
    for row in plan["rows"]:
        if row["error"]:
            print(f"  × {row['shot_id']}: {row['error']}")
        elif row["exists"] and not args.force:
            print(f"  - {row['shot_id']}: 跳过，clips/{row['shot_id']}.mp4 已存在")
        else:
            todo.append(row)
            tag = "覆盖" if row["exists"] else "新建"
            refs = row.get("ref_images") or []
            if not refs:
                extra = ""
            elif len(refs) == 1:
                extra = f"  参考图 {refs[0]}"
            else:
                # 两人同框的镜头要两张参考图，这里必须把张数显出来，
                # 只显示第一张会让人以为只用了一个角色
                extra = f"  参考图 {len(refs)} 张：{', '.join(refs)}"
            print(f"  + {row['shot_id']}: {row['frames']} 帧  seed={row['seed']}"
                  f"  [{row['mode']}] {row['template']}  [{tag}]{extra}")

    fatal = [r for r in plan["rows"] if r["error"]]
    if fatal:
        print(f"\n× {len(fatal)} 个镜头参数不合法，先修 shots.json。", file=sys.stderr)
        return ERR

    if not todo:
        print("\n没有需要生成的镜头。要强制重生成加 --force。")
        return OK

    print(f"\n合计 {len(todo)} 条，{sum(r['frames'] for r in todo)} 帧，"
          f"约 {sum(r['frames'] for r in todo) / float(cfg.get('generation.video.fps', 24)):.1f} 秒视频。")

    if not args.yes:
        print("\n这是 dry-run，**没有提交任何任务**。")
        print("生视频慢且计费，确认这份清单无误后再加 --yes 真跑。")
        return OK

    print("\n开始生成（串行，一条一条来）…\n")
    report = generate_stage.run(cfg, project_dir, only=args.only, force=args.force)
    ok, bad = generate_stage.summarize(report)

    for line in ok:
        print(line)
    if bad:
        print("\n失败：")
        for line in bad:
            print(line)

    patched = [r["shot_id"] for r in report.get("results", [])
               if (r.get("shots_json_patched") or {}).get("duration") is not None]
    if patched:
        print(f"\n! {len(patched)} 条镜头的帧数被模型抬到了上一档（固定量化行为，不是失败），"
              f"shots.json 的 duration 已按实测改正：{', '.join(patched)}")
        print("  排轨取的是 shots.json 的 duration，所以下一步要重建 manifest：")
        print(f"    vcp manifest {project_dir}")
        print("  align / keyphrase 不用重跑 —— 它们的时间取自真实音轨，和这里无关。")

    print(f"\n+ {project_dir / 'generate.json'}")
    print("  原始产物在 gen/，供后续工序使用的副本在 clips/。")
    return ERR if bad else OK


def cmd_textcheck(args) -> int:
    """扫项目里的片段，报哪些可能被模型烧进了文字。"""
    from . import textcheck as tc_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    report = tc_stage.scan(cfg, project_dir, only=args.only)
    ok, bad = tc_stage.summarize(report)

    print(f"烧字检测（{report['checked']} 条，阈值 {report['threshold']}）")
    print("  依据：MiniMax-H3 会把台词渲染进画面，而我们的字幕也在下方 → 成片双份字。\n")
    for line in ok:
        print(line)
    if bad:
        print("\n可疑：")
        for line in bad:
            print(line)

    print(f"\n+ {project_dir / 'textcheck.json'}")
    if report["suspect_count"]:
        print(f"\n{report['suspect_count']} 条可疑。误报的代价是多跑一次生成（~90 秒），"
              "漏报的代价是双份字幕流到客户 —— 所以阈值刻意取低。")
        print("建议：先肉眼确认；确实是烧字就换 seed 重生成（`vcp generate --force`），"
              "或直接 `vcp desub` 把字从像素上抹掉（不用重生成）。")
        return MISMATCH
    return OK


def cmd_desub(args) -> int:
    """第 2c' 段：把画面里烧进去的字从像素上抹掉（云端 VSR）。"""
    from . import desub as desub_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)

    # 开关必须自己开。开了开关，第 4 段排轨才会去用 clips_clean/ 里的产物；
    # 开关关着跑出来的产物没人会用，等于白跑 —— 所以这里直接拦下来。
    if not bool(cfg.get("desub.enabled", False)):
        print("× desub.enabled 是 false，没做任何事。", file=sys.stderr)
        print("  这个开关同时控制两件事：本命令跑不跑、第 4 段排轨用不用 clips_clean/。")
        print("  确实要去字，先在 config.yaml 里把 desub.enabled 改成 true。", file=sys.stderr)
        return ERR

    report = desub_stage.run(cfg, project_dir, only=args.only, force=args.force)
    ok, bad = desub_stage.summarize(report)

    how = ("让 OCR 在整帧里自己找字 —— 没找到就是干净，一个像素都不动"
           if report.get("locate") == "auto" else
           f"按 textcheck 的带子给 mask（上下各留 {report['coords_pad']}px）"
           + ("" if report["only_suspect"] else "，且不分可疑与否、全部处理"))
    print(f"第 2c' 段：去烧字（{report['mode']} —— {how}）")
    print("  依据：VSR 只重绘 mask 内的像素，其余画面一个字节不动；音轨原样 -codec copy 合回去。\n")
    for line in ok:
        print(line)
    if bad:
        print("\n失败：")
        for line in bad:
            print(line)

    print(f"\n+ {project_dir / 'desub.json'}")
    if bad:
        print("\n复检不过的产物已经被删掉了 —— 留着会被 resolved_clip 选中，"
              "等于把坏产物当好产物用。", file=sys.stderr)
        return ERR
    print("\n下一步：`vcp align`。去字产物带的是原音轨，时间基准没变，"
          "align/keyphrase 的既有结果都还有效。")
    return OK


def cmd_footage(args) -> int:
    """口播实拍素材入库。注意和混剪相反：保音轨、不截时长。"""
    from . import footage as footage_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)

    shots = footage_stage.footage_shots(project_dir, only=args.only)
    if not shots:
        print("× 没有 source_kind=footage 的镜头。", file=sys.stderr)
        return ERR

    media = Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
    explicit = Path(args.source) if args.source else None
    if explicit and len(shots) > 1:
        print(f"× 有 {len(shots)} 个 footage 镜头，--source 只能给一个。"
              "用 --only 限定，或把文件放进 incoming/ 按镜头 id 命名。", file=sys.stderr)
        return ERR

    fix = args.fix_duration or bool(cfg.get("footage.fix_duration", False))
    print(f"第 2a' 段：口播实拍素材入库（{len(shots)} 个镜头）")
    print(f"  规格化 {cfg.get('footage.normalize', True)}   自动修正时长 {fix}")
    print()

    results = []
    for shot in shots:
        try:
            src = footage_stage.find_source(project_dir, shot, explicit)
            results.append(footage_stage.import_one(cfg, media, project_dir, shot,
                                                    src, force=args.force,
                                                    fix_duration=fix))
        except Exception as exc:      # noqa: BLE001 —— 单条失败不拖累其余
            results.append({"shot_id": shot["id"], "error": str(exc)})

    ok, bad = footage_stage.summarize(results)
    for line in ok:
        print(line)
    if bad:
        print("\n失败：")
        for line in bad:
            print(line)
        return ERR

    print("\n下一步：`vcp align` 转文字并做逐字对齐。")
    print("  口播的时长以实拍为准 —— 上面若报时长不一致，先决定改镜头表还是重录。")
    return OK


def cmd_keyphrase(args) -> int:
    from . import keyphrase as kp_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    resolved = kp_stage.resolve(cfg, project_dir)
    items = resolved.get("keyphrases") or []

    print(f"第 3.5 段：重点句解析（{len(items)} 条）")
    if not items:
        print(f"  {resolved.get('note') or '没有重点句'}")
    for line in kp_stage.summarize(resolved):
        print(line)

    print(f"\n+ {project_dir / 'keyphrases_resolved.json'}")
    if items:
        print("下一步：`vcp manifest` 会把重点句从字幕里拆成独立段挂动画，"
              "并加一条音轨挂声音效果。")
    return OK


def cmd_manifest(args) -> int:
    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    result = manifest_stage.run(cfg, project_dir, out_name=args.out,
                                draft_name=args.draft_name)
    stats = result["stats"]
    draft = result["manifest"]["draft"]

    print(f"第 4 段：生成 manifest（{stats['shot_count']} 个镜头）")
    print(f"  草稿 {draft['name']}   {draft['width']}x{draft['height']} @{draft['fps']}fps")
    print(f"  视频段 {stats['video_segments']}  字幕段 {stats['text_segments']}"
          f"  总时长 {stats['total_seconds']}s")
    print(f"  用到超分片段 {stats['upscaled_clips_used']} 个")
    if stats["keyphrase_shots"]:
        print(f"  重点句镜头 {stats['keyphrase_shots']}")

    for w in result["warnings"]:
        print(f"  ! {w}")
    for p in result["problems"]:
        print(f"  × {p}", file=sys.stderr)

    if result["problems"]:
        # 有硬错误时 run() **不写盘**，所以这里没有路径可打印，
        # 上一次那份好的 manifest 也原样留着（不会被这份坏的覆盖）。
        print("\n有硬错误，**没有写 manifest**（上一次那份原样保留）。"
              "先修掉上面 × 开头的那些。", file=sys.stderr)
        return ERR

    print(f"\n+ {result['path']}")
    print("\n下一步：让模型调剪映 MCP 的 build_draft 落草稿，先 dry_run 试一遍：")
    print("  build_draft(manifest=<上面这个文件的 JSON>, dry_run=true)")
    print("  dry_run 通过后再传 false 真正发布到剪映草稿目录。")
    return OK


def cmd_stock(args) -> int:
    from . import stock as stock_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)

    rows = stock_stage.plan(cfg, project_dir, only=args.only)
    print("第 2a 段：混剪素材（Pexels 检索 + 规格化）")
    print(f"  数据源 {cfg.get('stock.provider')}   画布按 shots.json")
    print()

    todo = []
    for row in rows:
        if row["error"]:
            print(f"  × {row['shot_id']}: {row['error']}", file=sys.stderr)
        elif row["exists"] and not args.force:
            print(f"  - {row['shot_id']}: 跳过，clips/{row['shot_id']}.mp4 已存在")
        else:
            todo.append(row)
            print(f"  + {row['shot_id']}: 「{row['query']}」  需要 {row['duration']}s")

    if any(r["error"] for r in rows):
        print("\n有镜头的参数不合法，先修 shots.json。", file=sys.stderr)
        return ERR
    if not todo:
        print("\n没有需要下载的镜头。要重下加 --force。")
        return OK

    if not args.yes:
        print(f"\n这是 dry-run，**没有发起任何检索和下载**。加 --yes 真跑。")
        return OK

    print(f"\n开始检索下载（{len(todo)} 条）…\n")
    report = stock_stage.run(cfg, project_dir, only=args.only, force=args.force)
    ok, bad = stock_stage.summarize(report)
    for line in ok:
        print(line)
    if bad:
        print("\n失败：")
        for line in bad:
            print(line)

    print(f"\n+ {project_dir / 'stock.json'}")
    print("  原始下载在 gen/，规格化到画布的副本在 clips/。")
    return ERR if bad else OK


def cmd_t2i(args) -> int:
    """文生图：出角色底图。charset 是图生图编辑，底图得先由它出。"""
    from . import t2i as t2i_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)
    dest = Path(args.out)
    if not dest.is_absolute():
        dest = project_dir / dest

    print("文生图")
    print(f"  模型 {cfg.require('t2i.models.unet_name')}")
    print(f"  尺寸 {cfg.get('t2i.width')}x{cfg.get('t2i.height')}  "
          f"steps={cfg.get('t2i.steps')} cfg={cfg.get('t2i.cfg')}  seed={args.seed}")
    print(f"  提示词 {args.prompt[:80]}{'…' if len(args.prompt) > 80 else ''}")
    print()

    result = t2i_stage.generate(cfg, args.prompt, args.seed, dest)
    print(f"+ {dest}  ({result['bytes'] / 1024:.0f} KB，{result['seconds']}s)")
    print("\n下一步：`vcp charset --image 上面这张 --name <角色id>` 转成四视图人设图。")
    return OK


def cmd_charset(args) -> int:
    from . import charset as charset_stage

    project_dir = Path(args.project_dir)
    cfg = _cfg(project_dir)

    print(f"第 2c 段：生成四视图人设图（角色 {args.name}）")
    try:
        result = charset_stage.run(cfg, project_dir, Path(args.image), args.name)
    except (charset_stage.CharsetError, json.JSONDecodeError) as exc:
        print(f"× {exc}", file=sys.stderr)
        return ERR

    backend = result.get("backend") or {}
    print(f"  后端 {backend.get('mode')}  {backend.get('base')}")
    print()
    for line in result.get("notes") or []:
        print(f"  · {line}")
    print(f"\n+ {project_dir / result['out']}  ({result['bytes'] / 1024:.0f} KB，"
          f"{result['seconds']}s)")
    print("\n下一步：把这个路径填进 shots.json 的 ref_images，"
          "`vcp generate` 就会自动走 r2v。")
    return OK


def cmd_ui2api(args) -> int:
    """把 ComfyUI 界面里存的工作流转成 API 图，看看转出来是什么。

    用途：给人设图这类复杂工作流写模板时，先转出来确认参数落在正确的位置，
    不然写出来的模板可能整块是错的。
    """
    from . import comfyenv, ui2api as u2a

    src = Path(args.workflow)
    if not src.is_file():
        print(f"× 找不到工作流文件 {src}", file=sys.stderr)
        return ERR

    # 这些 JSON 带 UTF-8 BOM —— 用 utf-8（不带 sig）会直接解析失败
    try:
        workflow = json.loads(src.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        print(f"× {src.name} 不是合法 JSON：{exc}", file=sys.stderr)
        return ERR

    cfg = config.load(Path(args.project) if args.project else None)

    with comfyenv.open_comfy(cfg) as (client, info):
        print(f"从 {info['mode']} 的 ComfyUI 取 /object_info …")
        oi = client.object_info()

    try:
        graph, notes = u2a.convert(workflow, oi)
    except u2a.ConvertError as exc:
        print(f"× {exc}", file=sys.stderr)
        return ERR

    problems = u2a.verify(graph)

    print(f"\n转换出 {len(graph)} 个节点（这台 ComfyUI 共 {len(oi)} 种节点）")
    for n in notes:
        print(f"  · {n}")
    for pr in problems:
        print(f"  × {pr}", file=sys.stderr)

    if args.out:
        out = Path(args.out)
        artifacts.write_json(out, graph)
        print(f"\n+ {out}")

    if not args.quiet:
        print()
        for nid in sorted(graph, key=lambda x: int(x) if x.isdigit() else 0):
            node = graph[nid]
            print(f"#{nid:<4} {node['class_type']}")
            for k, v in node["inputs"].items():
                s = json.dumps(v, ensure_ascii=False)
                print(f"        {k} = {s if len(s) <= 78 else s[:78] + '…'}")

    return ERR if problems else OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vcp", description="video-cut-pipeline 脚本层")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="新建项目目录和 config.yaml")
    p.add_argument("project_id")
    p.add_argument("--work-root", help="项目根目录；不填则取配置里的 paths.work_root")
    p.add_argument("--task-type", choices=["aigc", "stock", "footage"], help="只作为注释写进配置")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 config.yaml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import-spec", help="把遗留的 spec_*.json 迁成 shots.json")
    p.add_argument("project_dir")
    p.add_argument("--spec", required=True)
    p.add_argument("--name")
    p.add_argument("--task-type", default="aigc", choices=["aigc", "stock", "footage"])
    p.set_defaults(func=cmd_import_spec)

    p = sub.add_parser("check", help="校验 shots.json")
    p.add_argument("project_dir")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("align", help="第 3 段：抽音轨 + ASR + 听检 → align/")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只处理这些镜头 id")
    p.set_defaults(func=cmd_align)

    p = sub.add_parser("generate", help="第 2b 段：镜头表 → ComfyUI 生成 clips/")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只生成这些镜头 id")
    p.add_argument("--force", action="store_true",
                   help="已有 clips/<id>.mp4 也重新生成（会覆盖，慎用）")
    p.add_argument("--yes", action="store_true",
                   help="真的提交。不加就是 dry-run，只打印清单")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("upscale", help="第 2.5 段：超分 clips/ → clips_sr/")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只处理这些镜头 id")
    p.set_defaults(func=cmd_upscale)

    p = sub.add_parser("stock", help="第 2a 段：混剪镜头 → Pexels 检索下载 → clips/")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只处理这些镜头 id")
    p.add_argument("--force", action="store_true", help="已有 clips/<id>.mp4 也重下")
    p.add_argument("--yes", action="store_true", help="真的检索下载。不加就是 dry-run")
    p.set_defaults(func=cmd_stock)

    p = sub.add_parser("t2i", help="文生图（角色底图）。charset 是图生图编辑，底图得先由它出")
    p.add_argument("project_dir")
    p.add_argument("--prompt", required=True)
    p.add_argument("--out", required=True, help="输出图片路径（相对项目目录或绝对路径）")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_t2i)

    p = sub.add_parser("charset", help="第 2c 段：角色图 → 四视图人设图（给 r2v 当参考图）")
    p.add_argument("project_dir")
    p.add_argument("--image", required=True, help="角色图路径（从既有片段抽一帧也行）")
    p.add_argument("--name", required=True, help="角色 id，产物写 assets/char_<name>_sheet.png")
    p.set_defaults(func=cmd_charset)

    p = sub.add_parser("ui2api",
                       help="把 ComfyUI 界面工作流转成 API 图（写模板时确认参数位置用）")
    p.add_argument("workflow", help="UI 格式的工作流 JSON")
    p.add_argument("--project", help="项目目录，用来读配置里的 ComfyUI 地址")
    p.add_argument("--out", help="把转换结果写成 JSON")
    p.add_argument("--quiet", action="store_true", help="只打印汇总，不逐个列节点")
    p.set_defaults(func=cmd_ui2api)

    p = sub.add_parser("textcheck",
                       help="检测画面里有没有模型烧进去的文字（交付级问题）")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只查这些镜头 id")
    p.set_defaults(func=cmd_textcheck)

    p = sub.add_parser("desub",
                       help="第 2c' 段：把画面里烧进去的字从像素上抹掉 → clips_clean/")
    p.add_argument("project_dir")
    p.add_argument("--only", nargs="+", help="只处理这些镜头 id")
    p.add_argument("--force", action="store_true", help="已有 clips_clean/<id>.mp4 也重做")
    p.set_defaults(func=cmd_desub)

    p = sub.add_parser("footage", help="第 2a' 段：口播实拍素材入库 → clips/（保音轨、不截时长）")
    p.add_argument("project_dir")
    p.add_argument("--source", help="实拍视频路径；不填则在 <项目>/incoming/ 里按镜头 id 找")
    p.add_argument("--only", nargs="+", help="只处理这些镜头 id")
    p.add_argument("--force", action="store_true", help="已有 clips/<id>.mp4 也覆盖")
    p.add_argument("--fix-duration", action="store_true",
                   help="把 shots.json 的 duration 改成实测值（口播时长以实拍为准）")
    p.set_defaults(func=cmd_footage)

    p = sub.add_parser("keyphrase",
                       help="第 3.5 段：keyphrases.json → 解析出时间和音频段")
    p.add_argument("project_dir")
    p.set_defaults(func=cmd_keyphrase)

    p = sub.add_parser("manifest", help="第 4 段：shots + align → manifest.json")
    p.add_argument("project_dir")
    p.add_argument("--out", default="manifest.json", help="输出文件名")
    p.add_argument("--draft-name", help="草稿名。默认取 shots.json 的 project 字段。"
                                        "想保留已有草稿就换个名字，例如 xxx_v2")
    p.set_defaults(func=cmd_manifest)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (artifacts.ArtifactError, config.ConfigError, RuntimeError) as exc:
        print(f"× {exc}", file=sys.stderr)
        return ERR


if __name__ == "__main__":
    sys.exit(main())
