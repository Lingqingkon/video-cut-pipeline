"""ffmpeg / ffprobe 封装。所有媒体探测和转码都从这里走。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class MediaError(Exception):
    pass


class Media:
    def __init__(self, ffmpeg: str, ffprobe: str):
        self.ffmpeg = self._resolve(ffmpeg, "ffmpeg")
        self.ffprobe = self._resolve(ffprobe, "ffprobe")

    @staticmethod
    def _resolve(exe: str, label: str) -> str:
        # 绝对路径直接检查存在性；否则当作 PATH 里的命令名
        if Path(exe).is_absolute():
            if not Path(exe).is_file():
                raise MediaError(f"配置里的 {label} 不存在：{exe}")
            return exe
        found = shutil.which(exe)
        if not found:
            raise MediaError(f"PATH 里找不到 {label}，也没有在配置里给绝对路径")
        return found

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise MediaError(
                f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd[:3])} …\n{proc.stderr[-800:]}"
            )
        return proc

    def probe(self, path: Path | str) -> dict:
        """探测媒体信息。返回 duration_sec / width / height / fps / has_audio 等。"""
        path = Path(path)
        if not path.is_file():
            raise MediaError(f"文件不存在：{path}")
        proc = self._run([
            self.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ])
        data = json.loads(proc.stdout or "{}")
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        num, _, den = (video.get("r_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0.0
        return {
            "duration_sec": float(fmt.get("duration") or 0.0),
            "size_bytes": int(fmt.get("size") or 0),
            "width": video.get("width"),
            "height": video.get("height"),
            "video_codec": video.get("codec_name"),
            "fps": round(fps, 3),
            "has_audio": bool(audio),
            "audio_codec": audio.get("codec_name"),
            "audio_channels": audio.get("channels"),
            "audio_sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        }

    def extract_audio(self, src: Path | str, dst: Path | str,
                      sample_rate: int = 16000, mono: bool = True) -> Path:
        """抽音轨为 wav。ASR 用 16k 单声道最省事，Whisper 内部就是 16k。"""
        src, dst = Path(src), Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        info = self.probe(src)
        if not info["has_audio"]:
            raise MediaError(f"{src} 没有音轨。AIGC 生成的镜头本该自带语音，这里为空说明生成侧有问题。")
        cmd = [self.ffmpeg, "-v", "error", "-y", "-i", str(src),
               "-vn", "-acodec", "pcm_s16le", "-ar", str(sample_rate)]
        if mono:
            cmd += ["-ac", "1"]
        cmd.append(str(dst))
        self._run(cmd)
        return dst

    def cut_audio(self, src: Path | str, dst: Path | str, start: float, end: float) -> Path:
        """按时间区间切出音频段。重点句挂声音效果的前置步骤。

        用 `-ss` + `-t`（时长）而不是 `-ss` + `-to`：两者都写在输入侧时，
        `-to` 的参考系在各版本 ffmpeg 里不一致（有的按原时间轴、有的按偏移后时间轴），
        `-t` 没有歧义。
        """
        if end <= start:
            raise MediaError(f"切音频的区间非法：start={start} end={end}")
        src, dst = Path(src), Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._run([
            self.ffmpeg, "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
            "-vn", "-acodec", "pcm_s16le", str(dst),
        ])
        return dst


def open_media(cfg) -> Media:
    return Media(cfg.require("ffmpeg.ffmpeg"), cfg.require("ffmpeg.ffprobe"))
