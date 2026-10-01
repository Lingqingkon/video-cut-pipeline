"""配置加载。

解析顺序（后者覆盖前者）：
  内置默认  <  用户级 ~/.dsh/video-cut-pipeline.yaml  <  <project-dir>/config.yaml

机器相关的绝对路径只出现在配置文件里，代码里不许写死。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

USER_CONFIG_NAME = "video-cut-pipeline.yaml"

DEFAULTS: dict = {
    "jianying": {
        "draft_root": "",
        "canvas": {"width": 1080, "height": 1920, "fps": 24},
        "video_track_name": "主画面",
        # 字幕样式。这套值是既有工程 manifest_a1.json 上验证过的，别随意改。
        # 但那份工程画布是 1080×1920，这里画布可能是 480×864，
        # 字号会不会跟着画布缩放还没验证过 —— 生成后要在剪映里肉眼看一次。
        "subtitle": {
            "track_name": "字幕",
            "style": {"size": 8, "align": 1},
            "border": {"color": [0, 0, 0], "width": 20},
            "clip": {"transform_y": -0.7},
        },
    },
    "ffmpeg": {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
    },
    "generation": {
        # 工作流模板（相对 skill 的 templates/ 目录，或绝对路径）。
        # 不把图写死在代码里：AutoDL 上是 MiniMaxH3Director、本机是 MiniMaxH3Easy，
        # 节点名完全不同，写死任何一个换台机器就废。
        "template": "minimax_h3_director.t2v.json",
        # 镜头带参考图（人设图）时用这个模板，走 r2v 参考主体生视频
        "template_r2v": "minimax_h3_director.r2v.json",
        "task_type": "t2v — 文生视频(Text to Video)",
        "sample_group": "采样设置",
        "timeout": 5400,
        # 无条件加到每条 prompt 上的抑制词缀（前缀/后缀）。
        # 用途：MiniMax-H3 会习惯性把台词烧进画面，做成生成器自动附加比
        # 靠镜头表每次记得写更确定。已经写过就不重复加。
        "prompt_prefix": "",
        "prompt_suffix": (
            "画面中不出现任何文字、字幕、标题、水印、logo 或图形叠加，纯画面。"
            "（no text, no subtitles, no captions, no watermark, no logo, "
            "no overlay graphics, clean frame）"
        ),
        # ComfyUI 里的模型文件名。必须和 /object_info 里列出的完全一致。
        # t2v/i2v/fl2v 用 fl2va；r2v/v2v/rv2v 用 ref2va —— 这是插件自己的要求。
        "models": {
            "unet": "",
            "unet_ref": "",
            "clip": "",
            "clip_type": "minimax",
            "video_vae": "",
            "audio_vae": "",
        },
        "video": {
            "width": 480,
            "height": 864,
            "fps": 24,
            "cfg": 1.0,
            "steps": 8,
            "seed_start": 1000,
            # auto = 帧数由镜头时长反推（frames = duration × fps）
            "frames_mode": "auto",
        },
        "character": {
            "sheet_workflow": "CharacterSheet_QuadView",
            "t2i_workflow": "Krea2_BoldFlat_Sunlit_T2I",
            "max_ref_images": 9,
            "ref_image_size": "match",
        },
    },
    "stock": {
        "provider": "auto",
        "api_key_env": "PEXELS_API_KEY",
        "fetcher_dir": "",
        # 跑 stockvid 用哪个 python；留空用当前解释器（流水线 venv 里有 httpx 就够）
        "python": "",
        "orientation": "portrait",
        "search_limit": 12,
        "target_height": 1280,
        "min_short_side": 0,
        "strict": False,
        # 混剪素材一般当 B-roll，旁白压在它上面，所以默认丢掉素材自带音轨
        "keep_audio": False,
        "timeout": 300,
        "download_timeout": 600,
    },
    "asr": {
        "engine": "faster_whisper",
        "model_dir": "",
        "device": "cpu",
        "compute_type": None,
        "allow_cpu_fallback": True,
        "language": "zh",
        "word_timestamps": True,
        "vad_filter": True,
        "initial_prompt": "以下是普通话的句子。",
        "mode": "asr_then_compare",
        "on_mismatch": "report",
        "extract_audio": True,
        "sample_rate": 16000,
        # coverage 低于 (1 - mismatch_ratio) 就判为念错
        "mismatch_ratio": 0.15,
    },
    "sfx": {
        "mode": "jianying_effect",
        "vip_available": True,
        "default_categories": ["scene", "tone"],
    },
    # ComfyUI 运行环境。本机还是远端全部收在这里，上层代码不关心。
    "comfyui": {
        "mode": "local",                 # local | remote
        "base_url": "http://127.0.0.1:8188",
        "local_root": "",
        "remote_root": "",
        "remote_listen_host": "127.0.0.1",
        "remote_listen_port": 6006,
        "tunnel_local_port": 6006,
    },
    "remote": {
        "host": "",
        "port": 22,
        "user": "root",
        "key_file": "",
        # 密码只从环境变量或 secrets 文件来，绝不写进项目配置
        "password_env": "VCP_SSH_PASSWORD",
        "secrets_file": "",
    },
    # 人设图（四视图）。工作流是 UI 格式，运行时转换成 API 图 —— 这样可以在
    # ComfyUI 界面里改工作流，流水线直接吃新版本。
    "charset": {
        "enabled": True,
        "workflow": "CharacterSheet_QuadView_krea2.json",
        # 覆盖工作流里存的模型名（工作流里可能是过时的旧名字）
        "unet_name": "",
        "clip_name": "",
        "vae_name": "",
        # 没有这个 LoRA 就把它内联掉。留空表示不旁路任何节点。
        "bypass_class_types": [],
        "instruction": "",
        "width": 1536,
        "height": 1024,
        "steps": 10,
        "cfg": 1.0,
        "seed": 0,
        "timeout": 1800,
        "filename_prefix": "vcp/charset",
    },
    # 重点句。判断"哪句是重点"由模型写 keyphrases.json；
    # 时间由 keyphrase.py 从对齐结果里查，不猜。
    # 文生图。给人设图链路补上"从文字描述出一个角色底图"这一环 ——
    # charset 是图生图编辑（必须有输入图），底图得先由它出。
    "t2i": {
        "template": "krea2_turbo_t2i.json",
        "models": {
            "unet_name": "krea2_turbo_fp8_scaled.safetensors",
            "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
            "clip_type": "krea2",
            "vae_name": "qwen_image_vae.safetensors",
        },
        # Turbo 模型：10 步、cfg 1。cfg=1 时负向提示词不参与。
        "width": 832,
        "height": 1216,
        "steps": 10,
        "cfg": 1.0,
        "negative": "",
        "timeout": 900,
    },
    # 检测模型有没有把文字烧进画面。
    # 实测 10 条标注样本定标：有字 score 0.186~0.216，无字最高 0.1244。
    # 阈值宁低勿高 —— 误报代价是多跑一次生成（~90 秒），漏报代价是双份字幕流到客户。
    "textcheck": {
        "threshold": 0.14,
        "retry_on_text": 0,      # >0 时，检出可疑就换 seed 重生成，最多重试这么多次
    },
    # 口播实拍素材入库。和混剪相反：保音轨、不截时长。
    "footage": {
        "normalize": True,      # 缩放到画布尺寸并统一帧率
        "fix_duration": False,  # 是否自动把 shots.json 的 duration 改成实测值
    },
    "keyphrase": {
        "sfx_pad": 0.2,                  # 音频段前后各留一点，免得掐头去尾
        "cuts_audio": True,              # 切出音频段（effects 只能挂音频段上）
        "sfx_volume": 1.0,
        "sfx_track_name": "重点句音效",
        # 这些名字是**实测可用**的：冲屏位移/卡拉OK 来自用户的字体特效样张，
        # 故障打字机/居中打字/模糊 来自既有工程 manifest_v2.json。
        "default_animation": {"category": "text_intro", "name": "冲屏位移",
                              "duration": "0.6s"},
        "default_sfx": {"category": "scene", "name": "回音"},
    },
    "upscale": {
        # **默认关**。开着的话 `resolved_clip()` 会优先用 clips_sr/，
        # 而画布未必跟着改 —— 那就正好是硬纪律 6 说的半套状态。
        # 480p 直出是既定做法；要更高分辨率就打开它、并把 jianying.canvas 一起改。
        "enabled": False,
        "model": "RealESRGAN_x4plus.pth",
        "target_width": 1080,
        "target_height": 1920,
        "method": "lanczos",
        # center = 按比例放大填满再居中裁切。源是 5:9、画布是 9:16，差 1.25%，
        # 不能用拉伸，也不能无脑留黑边。
        "crop": "center",
        "timeout": 3600,
    },
    "paths": {
        "work_root": "",
    },
}


class ConfigError(Exception):
    pass


def _deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并，overlay 覆盖 base。返回新对象，不改动入参。"""
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 的顶层必须是映射，实际是 {type(data).__name__}")
    return data


def user_config_path() -> Path:
    dsh_home = os.environ.get("DSH_HOME") or (Path.home() / ".dsh")
    return Path(dsh_home) / USER_CONFIG_NAME


class Config:
    """已合并的配置。用点号取值：cfg.get("asr.model_dir")。"""

    def __init__(self, data: dict, project_dir: Path | None = None, sources: list[str] | None = None):
        self._data = data
        self.project_dir = project_dir
        self.sources = sources or []

    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> str:
        value = self.get(dotted)
        if value in (None, "", []):
            raise ConfigError(
                f"配置缺项 {dotted}。请在项目 config.yaml 里补上（模板见 skill 的 config.example.yaml）。"
            )
        return value

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)


def load(project_dir: Path | str | None = None) -> Config:
    """加载合并后的配置。project_dir 为空时只用默认 + 用户级。"""
    sources = ["<defaults>"]
    merged = copy.deepcopy(DEFAULTS)

    user_path = user_config_path()
    if user_path.is_file():
        merged = _deep_merge(merged, read_yaml(user_path))
        sources.append(str(user_path))

    proj = Path(project_dir).resolve() if project_dir else None
    if proj is not None:
        proj_cfg = proj / "config.yaml"
        if proj_cfg.is_file():
            merged = _deep_merge(merged, read_yaml(proj_cfg))
            sources.append(str(proj_cfg))

    # 密钥单独挂到 secrets 下，优先级最低（配置里写的东西不该被密钥文件覆盖）。
    secrets_path = (merged.get("remote") or {}).get("secrets_file")
    if secrets_path:
        sp = Path(str(secrets_path)).expanduser()
        if sp.is_file():
            merged["secrets"] = read_yaml(sp)
            sources.append(f"{sp}  (secrets)")

    return Config(merged, project_dir=proj, sources=sources)
