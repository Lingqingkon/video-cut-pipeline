"""第 2c 段：生成四视图人设图。

为什么需要它
------------
r2v（参考主体生视频）已经能用，但参考图一直是手动给的。这一步把"角色 → 人设图"补上，
之后 `vcp generate` 就能自动走 r2v 做人物一致性。

工作流从哪来
------------
直接吃 ComfyUI 界面格式的 UI 工作流（`templates/` 下），**运行时转换成 API 图**。
好处是你可以把工作流拖进 ComfyUI 图形界面里改（加节点、调参数、旁路），保存后流水线
直接吃新版本，不用碰代码。转换逻辑在 `ui2api.py`。

定位节点不靠 id
---------------
UI 工作流一旦在界面里重新保存，节点 id 就可能变。所以这里一律**按 class_type 加连线关系**
定位：比如"喂给 KSampler.positive 的那个编码器"才是指令编码器 —— 同一个工作流里
`Krea2EditGroundedEncode` 有两个（正向一个、负向一个），光看类型分不出来。

旁路
----
没有那个 LoRA，所以 `LoraLoaderModelOnly` 要内联掉：把下游对它的引用改成指向它的上游。
不能简单删掉，否则下游会指向一个不存在的节点、提交时报"节点不存在"。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import artifacts, comfy, comfyenv, ui2api

SKILL_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = SKILL_ROOT / "templates"


class CharsetError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


# ── 图手术 ───────────────────────────────────────────────────────────────

def find_by_class(graph: dict, class_type: str) -> list[tuple[str, dict]]:
    return [(nid, n) for nid, n in graph.items() if n.get("class_type") == class_type]


def one_by_class(graph: dict, class_type: str) -> tuple[str, dict]:
    found = find_by_class(graph, class_type)
    if not found:
        raise CharsetError(f"工作流里没有 {class_type} 节点")
    if len(found) > 1:
        raise CharsetError(
            f"工作流里有 {len(found)} 个 {class_type} 节点，无法确定用哪个："
            f"{[nid for nid, _ in found]}"
        )
    return found[0]


def upstream(graph: dict, node: dict, input_name: str):
    """取某个输入连到的 (节点id, 节点)，没连线返回 None。"""
    ref = (node.get("inputs") or {}).get(input_name)
    if not isinstance(ref, list) or len(ref) != 2:
        return None
    src = str(ref[0])
    return (src, graph[src]) if src in graph else None


def bypass_class_types(graph: dict, class_types: list[str]) -> list[str]:
    """把指定类型的节点内联掉：下游对它的引用改指向它的上游，然后删掉它。

    透传输入按优先级找第一个存在的（LoRA 的 model、ModelPatch 的 model、编码器的 clip…）。
    """
    wanted = {str(t) for t in class_types}
    notes: list[str] = []
    for nid in [n for n, node in graph.items() if node.get("class_type") in wanted]:
        node = graph[nid]
        passthrough = None
        for key in ("model", "MODEL", "clip", "vae", "image", "images", "latent"):
            ref = (node.get("inputs") or {}).get(key)
            if isinstance(ref, list) and len(ref) == 2 and str(ref[0]) in graph:
                passthrough = [str(ref[0]), ref[1]]
                break

        if passthrough is None:
            # 没有可透传的输入 -> 只能删，下游会悬空（由 ui2api.verify 报出来）
            for other in graph.values():
                other["inputs"] = {
                    k: v for k, v in (other.get("inputs") or {}).items()
                    if not (isinstance(v, list) and str(v[0]) == nid)
                }
            del graph[nid]
            notes.append(f"旁路 {node['class_type']} #{nid}：没有可透传的输入，已删除")
            continue

        rewired = 0
        for other in graph.values():
            for key, value in list((other.get("inputs") or {}).items()):
                if isinstance(value, list) and str(value[0]) == nid:
                    other["inputs"][key] = passthrough
                    rewired += 1
        del graph[nid]
        notes.append(
            f"旁路 {node['class_type']} #{nid}：{rewired} 条下游引用改指向 #{passthrough[0]}"
        )
    return notes


def set_input(graph: dict, node_id: str, name: str, value) -> None:
    graph[node_id].setdefault("inputs", {})[name] = value


# ── 组装 ─────────────────────────────────────────────────────────────────

def resolve_workflow(cfg) -> Path:
    raw = cfg.get("charset.workflow")
    if not raw:
        raise CharsetError("配置里没设 charset.workflow")
    path = Path(str(raw))
    if not path.is_absolute():
        path = TEMPLATE_DIR / path
    if not path.is_file():
        raise CharsetError(f"找不到人设图工作流 {path}")
    return path


def build_graph(cfg, object_info: dict, image_name: str, seed: int | None = None,
                notes_out: list[str] | None = None) -> dict:
    import json as _json

    workflow_path = resolve_workflow(cfg)
    # 这些工作流 JSON 带 UTF-8 BOM
    workflow = _json.loads(workflow_path.read_text(encoding="utf-8-sig"))

    graph, notes = ui2api.convert(workflow, object_info)

    for line in bypass_class_types(graph, cfg.get("charset.bypass_class_types") or []):
        notes.append(line)

    # 模型文件名：工作流里存的可能是过时的旧名字，以配置为准
    for class_type, name_key in (("UNETLoader", "unet_name"),
                                 ("CLIPLoader", "clip_name"),
                                 ("VAELoader", "vae_name")):
        wanted = cfg.get(f"charset.{name_key}")
        if not wanted:
            continue
        for nid, node in find_by_class(graph, class_type):
            set_input(graph, nid, name_key, wanted)
            notes.append(f"#{nid} {class_type}.{name_key} -> {wanted}")

    # 指令编码器 = 喂给 KSampler.positive 的那个（负向那个不动）
    sampler_id, sampler = one_by_class(graph, "KSampler")

    pos = upstream(graph, sampler, "positive")
    if pos is None:
        raise CharsetError("KSampler.positive 没有连线，工作流结构不对")
    set_input(graph, pos[0], "prompt", str(cfg.get("charset.instruction") or ""))
    notes.append(f"#{pos[0]} {pos[1]['class_type']}.prompt <- 四视图指令")

    # 输出尺寸
    latent = upstream(graph, sampler, "latent_image")
    if latent is None:
        raise CharsetError("KSampler.latent_image 没有连线，工作流结构不对")
    set_input(graph, latent[0], "width", int(cfg.get("charset.width", 1536)))
    set_input(graph, latent[0], "height", int(cfg.get("charset.height", 1024)))
    notes.append(f"#{latent[0]} {latent[1]['class_type']} <- "
                 f"{cfg.get('charset.width')}x{cfg.get('charset.height')}")

    # 采样参数
    set_input(graph, sampler_id, "steps", int(cfg.get("charset.steps", 10)))
    set_input(graph, sampler_id, "cfg", float(cfg.get("charset.cfg", 1.0)))
    if seed is not None:
        set_input(graph, sampler_id, "seed", int(seed))
    notes.append(f"#{sampler_id} KSampler <- steps={cfg.get('charset.steps')} "
                 f"cfg={cfg.get('charset.cfg')} seed={seed}")

    # 输入图
    for nid, _node in find_by_class(graph, "LoadImage"):
        set_input(graph, nid, "image", image_name)
        notes.append(f"#{nid} LoadImage.image -> {image_name}")

    # 输出前缀
    for nid, _node in find_by_class(graph, "SaveImage"):
        set_input(graph, nid, "filename_prefix", cfg.get("charset.filename_prefix", "vcp/charset"))
        notes.append(f"#{nid} SaveImage.filename_prefix <- "
                     f"{cfg.get('charset.filename_prefix', 'vcp/charset')}")

    problems = ui2api.verify(graph)
    if problems:
        raise CharsetError("转换后的图有悬空连线：\n  - " + "\n  - ".join(problems))

    if notes_out is not None:
        notes_out.extend(notes)
    return graph


# ── 执行 ─────────────────────────────────────────────────────────────────

def run(cfg, project_dir: Path, image: Path, name: str) -> dict:
    project_dir = Path(project_dir)
    image = Path(image)
    if not image.is_file():
        raise CharsetError(f"找不到角色图 {image}")

    out_rel = f"assets/char_{name}_sheet.png"
    dest = project_dir / out_rel

    notes: list[str] = []
    result: dict = {"character": name, "source": str(image), "out": out_rel}

    with comfyenv.open_comfy(cfg) as (client, info):
        result["backend"] = info
        image_name = client.put_input(image, name=f"char_{name}{image.suffix.lower()}")
        graph = build_graph(cfg, client.object_info(), image_name,
                            seed=cfg.get("charset.seed"), notes_out=notes)

        started = time.time()
        prompt_id = client.queue(graph)
        entry = client.wait(prompt_id, timeout=float(cfg.get("charset.timeout", 1800)))
        result["seconds"] = round(time.time() - started, 1)

        files = comfy.collect_files(entry)
        images = [f for f in files
                  if (f.get("type") or "output") != "input"
                  and (f.get("filename") or "").lower().endswith((".png", ".jpg", ".webp"))]
        chosen = images[0] if images else None
        if chosen is None:
            raise CharsetError(f"工作流跑完但没有图片产物。history 里的文件：{files}")

        rel = client.output_rel(chosen)
        size = client.fetch(rel, dest)
        result.update({"comfy_file": rel, "bytes": size, "prompt_id": prompt_id})

    result["notes"] = notes
    return result
