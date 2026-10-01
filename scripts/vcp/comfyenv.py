"""按配置组装 ComfyUI 运行环境。

把"本机 / 远端"的差异全部收在这一层：隧道、SSH 连接、文件落地方式。
上层（upscale、将来的生成）只拿到一个 client，不关心后面是本机还是 AutoDL。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from . import comfy
from .remote import Remote


def resolve_password(cfg) -> str | None:
    """密码来源优先级：环境变量 > secrets 文件。skill 目录里永远不存密码。"""
    env_name = cfg.get("remote.password_env") or "VCP_SSH_PASSWORD"
    from_env = os.environ.get(env_name)
    if from_env:
        return from_env
    for key in ("secrets.remote.password", "secrets.autodl.password"):
        value = cfg.get(key)
        if value:
            return value
    return None


def secrets_file(cfg) -> Path | None:
    raw = cfg.get("remote.secrets_file")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


@contextmanager
def open_comfy(cfg, require_ready: bool = True):
    """yield (client, info)。用 with 保证隧道和 SSH 连接都被收干净。"""
    mode = str(cfg.get("comfyui.mode", "local")).lower()

    if mode == "local":
        base = cfg.get("comfyui.base_url") or "http://127.0.0.1:8188"
        client = comfy.ComfyClient(base, comfy.LocalStore(cfg.require("comfyui.local_root")))
        if require_ready and not client.wait_ready(seconds=20):
            client.close()
            raise comfy.ComfyError(
                f"本机 ComfyUI 没响应：{base}。先把它启动起来（见 references/install.md）。"
            )
        try:
            yield client, {"mode": "local", "base": base}
        finally:
            client.close()
        return

    if mode != "remote":
        raise comfy.ComfyError(f"comfyui.mode 只能是 local 或 remote，实际是 {mode!r}")

    remote = Remote(
        host=cfg.require("remote.host"),
        port=int(cfg.get("remote.port", 22)),
        user=cfg.get("remote.user", "root"),
        password=resolve_password(cfg),
        key_filename=cfg.get("remote.key_file") or None,
    )
    remote.connect()
    try:
        store = comfy.RemoteStore(remote, cfg.require("comfyui.remote_root"))
        listen_port = int(cfg.get("comfyui.remote_listen_port", 6006))
        with remote.tunnel(
            int(cfg.get("comfyui.tunnel_local_port", listen_port)),
            cfg.get("comfyui.remote_listen_host", "127.0.0.1"),
            listen_port,
        ) as base:
            client = comfy.ComfyClient(base, store)
            if require_ready and not client.wait_ready(seconds=30):
                client.close()
                raise comfy.ComfyError(
                    f"SSH 隧道通了，但远端 ComfyUI 没响应（{base}）。\n"
                    f"盒子上的 ComfyUI 可能没在跑，或没监听 {listen_port}。\n"
                    "启动命令参考 references/install.md 的「远端 ComfyUI」一节。"
                )
            try:
                yield client, {"mode": "remote", "base": base,
                               "host": cfg.get("remote.host")}
            finally:
                client.close()
    finally:
        remote.close()
