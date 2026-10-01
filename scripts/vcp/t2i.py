"""文生图。给人设图链路补上"从文字描述出一个角色底图"这一环。

为什么需要
----------
`vcp charset` 吃的是 **图生图编辑** 工作流（`Krea2Edit*`），**必须有输入图**。
所以要为一个新角色做人设图，得先有那个角色的一张底图 —— 这一步就是它。

完整链路：
    vcp t2i      文字描述 → 角色底图（一张，通常是正脸半身）
    vcp charset  角色底图 → 四视图人设图
    vcp generate 人设图当参考图 → r2v，保证同一支片子里是同一个人

实现上复用 generate 的模板替换（整串占位符保留原类型，数字不会变成字符串）
和 comfyenv（本机/远端统一）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import comfy, comfyenv
from .generate import substitute

SKILL_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = SKILL_ROOT / "templates"


class T2IError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


def resolve_template(cfg) -> Path:
    raw = cfg.get("t2i.template") or "krea2_turbo_t2i.json"
    path = Path(str(raw))
    if not path.is_absolute():
        path = TEMPLATE_DIR / path
    if not path.is_file():
        raise T2IError(f"找不到文生图模板 {path}")
    return path


def load_template(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # _ 开头的键是模板自带的说明，不能提交给 ComfyUI（会被当成节点）
    return {k: v for k, v in data.items() if not k.startswith("_")}


def build_graph(cfg, prompt: str, seed: int, prefix: str) -> dict:
    values = {
        "unet_name": cfg.require("t2i.models.unet_name"),
        "clip_name": cfg.require("t2i.models.clip_name"),
        "clip_type": cfg.get("t2i.models.clip_type", "krea2"),
        "vae_name": cfg.require("t2i.models.vae_name"),
        "prompt": prompt,
        "negative": cfg.get("t2i.negative") or "",
        "width": int(cfg.get("t2i.width", 832)),
        "height": int(cfg.get("t2i.height", 1216)),
        "steps": int(cfg.get("t2i.steps", 10)),
        "cfg": float(cfg.get("t2i.cfg", 1.0)),
        "seed": int(seed),
        "prefix": prefix,
    }
    return substitute(load_template(resolve_template(cfg)), values)


def generate(cfg, prompt: str, seed: int, dest: Path,
             out_prefix: str = "vcp/t2i") -> dict:
    """出一张图，存到 dest。返回报告。"""
    dest = Path(dest)
    graph = build_graph(cfg, prompt, seed, out_prefix)

    with comfyenv.open_comfy(cfg) as (client, info):
        started = time.time()
        prompt_id = client.queue(graph)
        entry = client.wait(prompt_id, timeout=float(cfg.get("t2i.timeout", 900)))
        elapsed = time.time() - started

        files = comfy.collect_files(entry)
        images = [f for f in files
                  if (f.get("type") or "output") != "input"
                  and (f.get("filename") or "").lower().endswith(
                      (".png", ".jpg", ".jpeg", ".webp"))]
        if not images:
            raise T2IError(f"工作流跑完但没有图片产物。history 里的文件：{files}")

        chosen = images[0]
        rel = client.output_rel(chosen)
        size = client.fetch(rel, dest)

    return {"backend": info, "prompt": prompt, "seed": seed,
            "out": str(dest), "bytes": size, "seconds": round(elapsed, 1),
            "comfy_file": rel, "prompt_id": prompt_id}
