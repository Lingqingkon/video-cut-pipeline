"""AutoDL 远程执行与端口转发。

密码之类的凭据不放这里，由 config 从 ~/.dsh/video-cut-pipeline.secrets.yaml
或环境变量读进来。skill 目录保持"拷走也不泄露"。
"""

from __future__ import annotations

import select
import socketserver
import threading
from contextlib import contextmanager


class RemoteError(RuntimeError):
    """继承 RuntimeError，好让 CLI 统一捕获成一行错误而不是甩一堆 traceback。"""
    pass


def _paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise RemoteError(
            "没装 paramiko，远程功能不可用。装一下：pip install paramiko\n"
            f"原始错误：{exc}"
        ) from exc
    return paramiko


class _TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        srv = self.server
        try:
            chan = srv.transport.open_channel(
                "direct-tcpip",
                (srv.remote_host, srv.remote_port),
                self.request.getpeername(),
            )
        except Exception:
            return
        if chan is None:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, chan], [], [], 0.5)
                if self.request in readable:
                    data = self.request.recv(8192)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in readable:
                    data = chan.recv(8192)
                    if not data:
                        break
                    self.request.sendall(data)
        except Exception:
            pass
        finally:
            for sock in (chan, self.request):
                try:
                    sock.close()
                except Exception:
                    pass


class _TunnelServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Remote:
    """一条 SSH 连接。支持 exec、SFTP 传输、以及本地端口转发。"""

    def __init__(self, host: str, port: int = 22, user: str = "root",
                 password: str | None = None, key_filename: str | None = None,
                 timeout: int = 25):
        self.host = host
        self.port = int(port)
        self.user = user
        self._password = password
        self._key_filename = key_filename
        self._cli = None

    # ── 连接管理 ──────────────────────────────────────────────────────

    def connect(self) -> "Remote":
        paramiko = _paramiko()
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cli.connect(
                hostname=self.host, port=self.port, username=self.user,
                password=self._password, key_filename=self._key_filename,
                timeout=25, banner_timeout=25, auth_timeout=25,
            )
        except Exception as exc:
            raise RemoteError(
                f"连不上 {self.user}@{self.host}:{self.port} —— {type(exc).__name__}: {exc}\n"
                "AutoDL 实例每次开机 host/port 都会变，关机后原地址就失效，先确认地址。"
            ) from exc
        self._cli = cli
        return self

    def close(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            finally:
                self._cli = None

    def __enter__(self) -> "Remote":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def transport(self):
        if self._cli is None:
            raise RemoteError("还没连接")
        return self._cli.get_transport()

    # ── 执行 ──────────────────────────────────────────────────────────

    def run(self, cmd: str, timeout: int = 300) -> str:
        """执行命令，返回合并后的输出。命令失败不抛异常，由调用方判断内容。"""
        if self._cli is None:
            raise RemoteError("还没连接")
        _in, out, err = self._cli.exec_command(cmd, timeout=timeout)
        parts = []
        for fh in (out, err):
            try:
                parts.append(fh.read().decode("utf-8", "replace"))
            except Exception:
                # 后台进程持有 channel 时 read 可能超时；不是致命问题
                parts.append("")
        return "".join(parts)

    def run_detached(self, cmd: str, log_path: str) -> str:
        """后台启动一个长期进程。

        三个流都要重定向，**包括 stdin**：只重定向 stdout/stderr 的话后台进程仍持有
        stdin，paramiko 的 channel 永远不关闭，read 必然超时。
        """
        wrapped = (
            f"setsid nohup bash -lc {_quote(cmd)} "
            f"> {_quote(log_path)} 2>&1 < /dev/null & echo launched"
        )
        return self.run(wrapped, timeout=60)

    def https_code(self, url: str, timeout: int = 8) -> str:
        """取 HTTP 状态码，用来探测远端服务是否就绪。"""
        return self.run(
            f"curl -s --max-time {timeout} -o /dev/null -w '%{{http_code}}' {_quote(url)}"
        ).strip()

    def run_python(self, script: str, interpreter: str = "/root/miniconda3/bin/python",
                   timeout: int = 300, remote_path: str = "/tmp/_vcp_remote.py") -> str:
        """把脚本 base64 传过去执行。

        远程路径不要用 `python -c '...'`：脚本里的引号会和 shell 引号打架，
        踩过一次（单引号把脚本截断，报 NameError）。
        """
        import base64
        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        cmd = (
            f"echo {payload} | base64 -d > {_quote(remote_path)} && "
            f"{_quote(interpreter)} {_quote(remote_path)}"
        )
        return self.run(cmd, timeout=timeout)

    # ── 文件传输 ──────────────────────────────────────────────────────

    def _sftp(self):
        if self._cli is None:
            raise RemoteError("还没连接")
        return self._cli.open_sftp()

    def put(self, local: str, remote: str) -> None:
        sftp = self._sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()

    def get(self, remote: str, local: str) -> None:
        from pathlib import Path
        sftp = self._sftp()
        try:
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote, str(local))
        finally:
            sftp.close()

    def stat_size(self, remote: str) -> int:
        """返回远端文件字节数，不存在返回 -1。"""
        out = self.run(f"stat -c %s {_quote(remote)} 2>/dev/null || echo -1").strip()
        try:
            return int(out.splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    # ── 端口转发 ──────────────────────────────────────────────────────

    @contextmanager
    def tunnel(self, local_port: int, remote_host: str = "127.0.0.1",
               remote_port: int = 6006):
        """把远端端口映射到本机，yield 一个可直接用的 base URL。

        ComfyUI 在某些部署里监听 0.0.0.0，但直接暴露公网端口不稳妥也不一定通，
        走 SSH 隧道更可靠，而且换机器不用改 AutoDL 的端口配置。
        """
        server = _TunnelServer(("127.0.0.1", int(local_port)), _TunnelHandler)
        server.transport = self.transport
        server.remote_host = remote_host
        server.remote_port = int(remote_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{local_port}"
        finally:
            server.shutdown()
            server.server_close()


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
