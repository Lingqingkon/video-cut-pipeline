"""ComfyUI 的 HTTP API 客户端，以及输入/输出文件的落地方式。

设计要点：**文件和 HTTP 分开**。
  - 图提交、进度查询走 HTTP（/prompt、/history、/object_info）
  - 素材进 input/、产物出 output/ 走文件系统

这样同一套代码既能驱动本机 ComfyUI（同一个盘，直接拷），也能驱动 AutoDL
（SFTP 拷），不用去赌 /upload 接口对不同媒体类型的支持。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx


class ComfyError(RuntimeError):
    """继承 RuntimeError，好让 CLI 统一捕获成一行错误而不是甩一堆 traceback。"""
    pass


# ── 文件落地：本机 vs 远端 ────────────────────────────────────────────────

class LocalStore:
    """本机 ComfyUI。root 是 ComfyUI 安装目录。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _p(self, rel: str) -> Path:
        return self.root / rel

    def put(self, local_path: str | Path, rel: str) -> str:
        dst = self._p(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(local_path), str(dst))
        return rel

    def get(self, rel: str, local_path: str | Path) -> None:
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(self._p(rel)), str(dst))

    def size(self, rel: str) -> int:
        p = self._p(rel)
        return p.stat().st_size if p.is_file() else -1

    def exists(self, rel: str) -> bool:
        return self._p(rel).is_file()


class RemoteStore:
    """远端 ComfyUI（走 SFTP）。remote 是 vcp.remote.Remote 实例。"""

    def __init__(self, remote, root: str = "/root/autodl-tmp/ComfyUI"):
        self.remote = remote
        self.root = root.rstrip("/")

    def _p(self, rel: str) -> str:
        return f"{self.root}/{rel}"

    def put(self, local_path: str | Path, rel: str) -> str:
        self.remote.run(f"mkdir -p $(dirname {shlex_q(self._p(rel))})")
        self.remote.put(str(local_path), self._p(rel))
        return rel

    def get(self, rel: str, local_path: str | Path) -> None:
        self.remote.get(self._p(rel), str(local_path))

    def size(self, rel: str) -> int:
        return self.remote.stat_size(self._p(rel))

    def exists(self, rel: str) -> bool:
        return self.size(rel) >= 0


def shlex_q(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


# ── HTTP 客户端 ──────────────────────────────────────────────────────────

class ComfyClient:
    def __init__(self, base_url: str, store, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.store = store
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ComfyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 端点 ──────────────────────────────────────────────────────────

    def system_stats(self) -> dict:
        r = self._client.get(f"{self.base}/system_stats")
        r.raise_for_status()
        return r.json()

    def object_info(self) -> dict:
        r = self._client.get(f"{self.base}/object_info", timeout=120)
        r.raise_for_status()
        return r.json()

    def wait_ready(self, seconds: int = 120, poll: float = 5.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.system_stats()
                return True
            except Exception:
                time.sleep(poll)
        return False

    def queue(self, graph: dict, client_id: str = "vcp") -> str:
        payload = {"prompt": graph, "client_id": client_id}
        r = self._client.post(f"{self.base}/prompt", json=payload, timeout=120)
        if r.status_code != 200:
            # ComfyUI 的校验错误在 body 里，比状态码有用得多，必须带出来
            raise ComfyError(f"提交工作流被拒（HTTP {r.status_code}）：{_explain(r)}")
        body = r.json()
        pid = body.get("prompt_id")
        if not pid:
            raise ComfyError(f"提交成功但没返回 prompt_id：{body}")
        return pid

    def wait(self, prompt_id: str, timeout: float = 3600.0, poll: float = 2.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self._client.get(f"{self.base}/history/{prompt_id}", timeout=60)
            if r.status_code == 200:
                entry = (r.json() or {}).get(prompt_id)
                if entry:
                    status = (entry.get("status") or {})
                    if status.get("status_str") == "error":
                        raise ComfyError(f"工作流执行失败：{_explain_status(status)}")
                    if status.get("completed") or entry.get("outputs"):
                        return entry
            time.sleep(poll)
        raise ComfyError(f"等 {prompt_id} 超过 {timeout:.0f} 秒仍未完成")

    # ── 文件 ──────────────────────────────────────────────────────────

    def put_input(self, local_path: str | Path, name: str | None = None) -> str:
        """把素材放进 ComfyUI 的 input/，返回可在工作流里引用的文件名。"""
        local_path = Path(local_path)
        name = name or local_path.name
        self.store.put(local_path, f"input/{name}")
        return name

    def output_rel(self, item: dict) -> str:
        """把 history 里的文件条目转成相对 ComfyUI 根目录的路径。"""
        sub = item.get("subfolder") or ""
        fname = item.get("filename") or ""
        typ = item.get("type") or "output"
        sub_path = f"{typ}/{fname}" if not sub else f"{typ}/{sub}/{fname}"
        return sub_path

    def fetch(self, rel: str, local_path: str | Path) -> int:
        """下载产物，返回字节数。"""
        self.store.get(rel, local_path)
        return Path(local_path).stat().st_size


def collect_files(entry: dict) -> list[dict]:
    """从 history 条目里捞出所有产物文件条目。

    不同节点把产物放在不同键下（images / video / videos / gifs / audio ...），
    所以不猜键名，直接递归找带 filename 的字典。
    """
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if "filename" in node and isinstance(node["filename"], str):
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(entry.get("outputs") or {})
    return found


def pick_output_video(files: list[dict], exts=(".mp4", ".mkv", ".webm", ".mov")) -> dict | None:
    """挑出真正的产物文件。

    **必须排除 type=="input"**：LoadVideo / LoadImage 这类加载节点会把自己读的
    输入文件也回显进 history 的 outputs 里（`{"filename": "a1_c2.mp4",
    "type": "input"}`）。无脑取第一个 .mp4 会拿到输入文件，
    看起来"跑成功了"，实际什么都没做。这个坑在超分上踩过一次，
    所以提到这里让生成也走同一份逻辑。
    """
    produced = [f for f in files if (f.get("type") or "output") != "input"]
    pool = produced or files
    videos = [f for f in pool
              if (f.get("filename") or "").lower().endswith(tuple(exts))]
    return (videos or pool or [None])[0]


def _explain(response) -> str:
    try:
        body = response.json()
    except Exception:
        return (response.text or "")[:600]
    err = body.get("error")
    detail = body.get("node_errors") or body.get("details")
    parts = []
    if err:
        parts.append(str(err))
    if detail:
        parts.append(json.dumps(detail, ensure_ascii=False)[:900])
    return " | ".join(parts) or json.dumps(body, ensure_ascii=False)[:600]


def _explain_status(status: dict) -> str:
    for msg in status.get("messages") or []:
        if isinstance(msg, list) and len(msg) == 2 and isinstance(msg[1], dict):
            if msg[0] in ("execution_error", "execution_interrupted"):
                info = msg[1]
                return (f"{info.get('node_type')} 节点出错：{info.get('exception_type')} "
                        f"{info.get('exception_message')}")
    return json.dumps(status, ensure_ascii=False)[:600]
