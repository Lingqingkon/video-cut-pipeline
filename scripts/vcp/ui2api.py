"""ComfyUI UI 工作流 → API 图 的转换。

为什么需要它
------------
`/prompt` 只吃 API 格式（`{"1": {"class_type": ..., "inputs": {...}}}`），
而人在 ComfyUI 界面里编辑并保存的是 UI 格式（`nodes` + `links` + `widgets_values`）。

与其手写维护一份 API 格式的模板，不如**把 UI 工作流原样存起来，运行时转换**：
用户可以在 ComfyUI 图形界面里改工作流（加节点、调参数、旁路），保存后流水线直接吃新版本，
不用碰任何代码。这也让"用户自带工作流"成为可能。

两个坑
------
1. **`widgets_values` 里混着前端独有的值。** 例如 KSampler 的 seed 后面会跟一个
   `control_after_generate`（"randomize"/"fixed"…），它**不是**后端输入，但占着位置。
   无脑按位置对齐会让 seed 之后的所有参数整体错位。
   判断依据是 schema 元数据里的 `control_after_generate`。

2. **`inputs` 数组只列连线输入，不列 widget 输入。** 所以 widget 名只能从
   `/object_info` 的 schema 拿，顺序是 required 在前、optional 在后。
"""

from __future__ import annotations

import copy

# 这些是"值型"输入（由 widgets_values 提供）；其余字面量类型都是连线输入。
WIDGET_SCALARS = {"INT", "FLOAT", "STRING", "BOOLEAN"}

# ComfyUI 的旁路/静音模式
MODE_ACTIVE = 0
MODE_MUTED = 2
MODE_BYPASS = 4

# 前端独有的节点：只存在于浏览器里，永远不会出现在 /object_info。
# 它们对出图没有影响，跳过就行。
# （Note 这条是实测撞出来的；Set/Get 是前端虚拟节点；Fast Groups Bypasser 是 rgthree 的
#   纯 web 节点 —— 单独看 aux_id 区分不出来，因为 Note 的 aux_id 是 None。）
FRONTEND_ONLY = {
    "Note", "MarkdownNote", "SetNode", "GetNode",
    "Fast Groups Bypasser (rgthree)",
}

# 纯连线转接节点：没有后端实现，但会把输入原样透传到输出。
# 不能简单跳过 —— 得回溯到它真正的源头，否则下游连线会指向一个不存在的节点。
PASSTHROUGH = {"Reroute"}


def _resolve_origin(node_id, slot, by_id: dict, links: dict, depth: int = 0):
    """顺着 Reroute 一路回溯到真正的源节点，返回 (node_id, slot) 或 None。"""
    if depth > 32:
        return None
    node = by_id.get(node_id)
    if not node or node.get("type") not in PASSTHROUGH:
        return (node_id, slot)
    # Reroute 把输入原样透传。多输入的 Reroute 按 slot 取，取不到就用第一个有连线的。
    picks = [inp for inp in (node.get("inputs") or []) if inp.get("link") is not None]
    if not picks:
        return None
    chosen = None
    for inp in picks:
        if inp.get("slot_index") == slot:
            chosen = inp
            break
    chosen = chosen or picks[0]
    upstream = links.get(chosen.get("link"))
    if not upstream:
        return None
    return _resolve_origin(upstream[0], upstream[1], by_id, links, depth + 1)


class ConvertError(RuntimeError):
    """继承 RuntimeError，让 CLI 统一报一行错误。"""


def is_widget_type(t) -> bool:
    if isinstance(t, list):
        return True                      # COMBO 用列表枚举
    if not isinstance(t, str):
        return False
    return t in WIDGET_SCALARS or t.startswith("COMFY_DYNAMIC")


def widget_plan(schema: dict) -> list[tuple[str, bool]]:
    """按 schema 声明顺序列出 widget 输入，并标出"后面是否跟一个前端独有值"。

    返回 [(输入名, 是否跳过下一个值)]。required 在前、optional 在后 —— 这正是
    ComfyUI 前端给 widgets_values 排序的方式。
    """
    plan: list[tuple[str, bool]] = []
    inputs = (schema or {}).get("input") or {}
    for group in ("required", "optional"):
        for key, spec in (inputs.get(group) or {}).items():
            if not isinstance(spec, list) or not spec:
                continue
            if not is_widget_type(spec[0]):
                continue
            meta = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            # seed 这类 INT 会在后面插一个 control_after_generate
            plan.append((key, bool(meta.get("control_after_generate"))))
    return plan


def _link_table(workflow: dict) -> dict:
    """兼容两种 links 写法：list[dict] 和 list[list]。"""
    table = {}
    for link in workflow.get("links") or []:
        if isinstance(link, dict):
            table[link.get("id")] = (link.get("origin_id"), link.get("origin_slot"))
        elif isinstance(link, (list, tuple)) and len(link) >= 3:
            table[link[0]] = (link[1], link[2])
    return table


def convert(workflow: dict, object_info: dict) -> tuple[dict, list[str]]:
    """返回 (API 图, 提示清单)。提示是"跳过了什么"，不是错误。"""
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        raise ConvertError("工作流里没有 nodes 数组 —— 这看着不像 ComfyUI 的 UI 格式")

    links = _link_table(workflow)
    by_id = {n.get("id"): n for n in nodes}
    # 哪些节点是"连着线"的：有连线就说明它参与出图，缺失就是真错误
    connected: set = set()
    for origin, _slot in links.values():
        connected.add(origin)
    for node in nodes:
        for inp in node.get("inputs") or []:
            if inp.get("link") is not None:
                connected.add(node.get("id"))

    graph: dict = {}
    notes: list[str] = []

    for node in nodes:
        nid = node.get("id")
        ntype = node.get("type")
        mode = node.get("mode") or MODE_ACTIVE

        if not ntype:
            notes.append(f"#{nid}: 没有 type（子图实例？）—— 子图不受支持，跳过")
            continue
        if mode in (MODE_MUTED, MODE_BYPASS):
            notes.append(f"#{nid} {ntype}: mode={mode}（旁路/静音），跳过")
            continue

        schema = object_info.get(ntype)
        if schema is None:
            if ntype in FRONTEND_ONLY:
                notes.append(f"#{nid} {ntype}: 前端独有，跳过")
                continue
            if ntype in PASSTHROUGH:
                notes.append(f"#{nid} {ntype}: 纯连线转接，已把下游回溯到真正源头")
                continue
            if nid not in connected:
                notes.append(f"#{nid} {ntype}: 未连接任何线，跳过")
                continue
            raise ConvertError(
                f"#{nid}: 节点类型 {ntype!r} 在这个 ComfyUI 上不存在，但它还连着线。\n"
                "要么装对应的节点包，要么在界面里把它旁路掉再重新导出工作流。"
            )

        values = list(node.get("widgets_values") or [])
        vi = 0
        inputs: dict = {}

        # widget 值
        for name, skip_next in widget_plan(schema):
            if vi < len(values):
                inputs[name] = values[vi]
                vi += 1
            if skip_next:
                vi += 1                 # 跳过 control_after_generate 之类的前端值

        # 连线输入（覆盖同名 widget —— 连了线就以线为准）
        for inp in node.get("inputs") or []:
            name = inp.get("name")
            link_id = inp.get("link")
            if link_id is None or inp.get("widget"):
                continue
            origin = links.get(link_id)
            if not origin:
                continue
            resolved = _resolve_origin(origin[0], origin[1], by_id, links)
            if resolved is None:
                notes.append(f"#{nid} {ntype}.{name}: 上游是空的 Reroute，这条线被丢掉")
                continue
            inputs[name] = [str(resolved[0]), resolved[1]]

        graph[str(nid)] = {"class_type": ntype, "inputs": inputs}

    return graph, notes


def verify(graph: dict) -> list[str]:
    """自检：连线指向的节点必须存在，而且下标不能越界。

    这个检查很值：悬空连线提交给 ComfyUI 会报"节点不存在"，
    但错的是"哪个节点不见了"，反查很费劲。
    """
    problems: list[str] = []
    for nid, node in graph.items():
        for name, value in (node.get("inputs") or {}).items():
            if not isinstance(value, list) or len(value) != 2:
                continue
            src = str(value[0])
            if src not in graph:
                problems.append(
                    f"#{nid} {node.get('class_type')} 的 {name} 指向不存在的节点 #{src}"
                    "（源节点被旁路或删掉了？）"
                )
    return problems
