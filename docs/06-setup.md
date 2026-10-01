# 从零搭起来

## 系统要求

- **Windows** —— 剪映只有 Windows / macOS 版，Linux 上整条草稿写入环节不成立
- Python 3.10+
- 一张能跑 `faster-whisper` 的显卡（CPU 也能跑，慢）
- 一个 GPU 环境跑 ComfyUI（本机或云实例都行）

---

## 外部依赖

| 依赖 | 作用 | 少了会怎样 |
|---|---|---|
| **剪映专业版** | 打开草稿、人工导出 | 交付不了成片 |
| **剪映 MCP** | 写草稿的唯一通道 | 第 4/5 段完全做不了 |
| **ComfyUI + MiniMax-H3** | 生图生视频 | AIGC 分支做不了 |
| **ffmpeg / ffprobe** | 抽音轨、规格化、抽帧 | 第 3 段做不了 |
| **CTranslate2 格式的 Whisper 模型** | ASR | 听检和对齐做不了 |
| **Pexels API key** | 混剪素材检索 | 混剪分支只能退到免 key 的源 |

这些**不随仓库走**，目标机器要自己装。

---

## Python 环境

建一个**独立 venv**，不要装进 ComfyUI 的环境——`faster-whisper` 会拖一堆依赖，装进去有搞坏 ComfyUI 的风险。

```powershell
cd scripts
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r ..\requirements.txt
```

`requirements.txt` 里 `PyYAML` 是配置解析用的，`faster-whisper` 是 ASR 用的。

---

## GPU 还是 CPU

配置项 `asr.device` 和 `asr.compute_type`：

| 场景 | device | compute_type |
|---|---|---|
| NVIDIA 卡 | `cuda` | `float16` |
| 只用 CPU | `cpu` | `int8` |

### 走 GPU 的关键坑

**`faster-whisper` 底层是 CTranslate2，不是 PyTorch**，所以它**不会**用 torch 自带的 CUDA 库。它自己去找 `cublas64_12.dll` 和 `cudnn_*64_9.dll`。

两条路：

1. **系统装了完整 CUDA toolkit 12 + cuDNN 9** —— 直接能跑。
2. **没装（大多数情况）** —— 靠 pip 的 `nvidia-cublas-cu12` 和 `nvidia-cudnn-cu12` 提供。这两个 wheel 会把 DLL 放在 `site-packages\nvidia\*\bin\`，而**那个目录不在默认 DLL 搜索路径里**，所以 `align.py` 里的 `_prepare_cuda_dlls()` 会主动把它们加进搜索路径和 `PATH`。

第 2 条路的好处是**不需要系统级 CUDA**，pip 就能搞定，移植性最好。

驱动版本不受限：CUDA 12 的运行时能在更新的驱动上跑（新驱动向后兼容）。

`allow_cpu_fallback: true` 时，GPU 起不来会退回 CPU 并把原因打出来，而不是直接崩。**这是兜底不是解决方案——看到退回的提示要去修。**

---

## Whisper 模型

必须是 **CTranslate2 格式**的目录，含：

```
<model_dir>/
├── model.bin
├── config.json
├── tokenizer.json
└── vocabulary.txt        （可选）
```

从 HuggingFace 的 `Systran/faster-whisper-*` 仓库取，或本地已有的 whisper 权重用 `ct2-transformers-converter` 转。

**`medium` 的中文效果明显好于 `small`**，GPU 上速度差别可以接受。

> ⚠️ 这是**另一个格式**的模型，不是 `openai/whisper` 的 PyTorch 权重。pip 的 `faster-whisper` 读不了后者。

---

## ffmpeg

配置项 `ffmpeg.ffmpeg` / `ffmpeg.ffprobe`。可以给绝对路径，也可以只写命令名（走 PATH）。

需要一个 **full build**（含 ffprobe 同版本）。不要用某些软件附带的精简版。

`Media` 类会检查绝对路径是否存在，不存在直接报错，而不是等到转码时才炸。

---

## ComfyUI：本机还是远端

配置在 `comfyui.mode`：

| 取值 | 用法 |
|---|---|
| `local` | 直接连本机 ComfyUI（`base_url`），文件走同一个盘直接拷 |
| `remote` | 连云实例。`vcp` 自己开 SSH 隧道，文件走 SFTP |

远端模式下 `vcp upscale` 会做的事：SSH 连上去 → 本地端口转发到盒子的 ComfyUI → 把片段 SFTP 到 `input/` → 提交工作流 → 轮询 `/history` → 把产物 SFTP 下回来。

远端部署细节见 [`03-remote-gpu.md`](03-remote-gpu.md)。

### 超分模型要自己放

`models/upscale_models/` 默认是空的，要放 `RealESRGAN_x4plus.pth`。

来源：`https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth`

下完**核对字节数**（67040989）——`curl` 中断会留下半截文件，而半截的 `.pth` 加载时才报错。

超分依赖 ComfyUI 的核心节点：`LoadVideo` / `GetVideoComponents` / `UpscaleModelLoader` / `ImageUpscaleModel` / `ImageScale` / `CreateVideo` / `SaveVideo`。老版本 ComfyUI 不一定有这些，开工前用 `/object_info` 对一遍。

---

## 密钥放哪

**不放仓库，不放项目 config.yaml。** 两个来源，按优先级：

1. 环境变量（`remote.password_env`，默认 `VCP_SSH_PASSWORD`）
2. `remote.secrets_file`（默认 `~/.dsh/video-cut-pipeline.secrets.yaml`）

```yaml
# ~/.dsh/video-cut-pipeline.secrets.yaml
autodl:
  host: <your-instance>.seetacloud.com
  port: <port>
  user: root
  password: <密码>
```

有 SSH 密钥就填 `remote.key_file`，优先于密码。

云实例**每次开机 host/port 都会变**，关机后原地址立即失效——连不上先怀疑这个。

Pexels 的 key 走环境变量 `PEXELS_API_KEY`。

---

## 装完怎么验

```powershell
cd scripts

# 1. CLI 能不能起来
.venv\Scripts\python.exe -m vcp --help

# 2. 建个项目，确认目录和配置
.venv\Scripts\python.exe -m vcp init demo --work-root <WORK_ROOT>

# 3. 放一条真实 clip 和它的 shots.json 进去，跑对齐
#    这一步会真正加载 Whisper 模型、抽音轨、调 GPU
.venv\Scripts\python.exe -m vcp check  <项目目录>
.venv\Scripts\python.exe -m vcp align  <项目目录>

# 4. 超分（需要 ComfyUI 在跑）
.venv\Scripts\python.exe -m vcp upscale <项目目录> --only <一个镜头id>
```

- `align` 成功会写 `audio/_asr/*.wav` 和 `align/*.json`。**第一次跑要盯着有没有出现「GPU 加载失败，退回 CPU」**——出现了就说明 CUDA 运行时没配好。
- `upscale` 成功会写 `clips_sr/<shot-id>.mp4` 和 `upscale.json`。**必须检查输出里的分辨率那一列真的变了**——如果显示 `480x864 -> 480x864`，说明挑到了输入文件而不是产物。

---

## 接剪映 MCP

剪映 MCP 是一个独立的 MCP 服务进程，需要在 Agent 的 composition 里注册（配置示例见 [`02-mcp-toolchain.md`](02-mcp-toolchain.md)）。

注册后先调 `get_jianying_capabilities` 看它检测到的剪映版本，确认通道是通的。

---

## 迁移到另一台机器时容易漏的

1. **CuDNN / cublas 的 wheel 是平台相关的**，Linux 目标机同样要装 `nvidia-*` 包（名字一样）。
2. **`requirements.txt` 里带 `nvidia-*` 的行在纯 CPU 机器上会白下几百 MB**，可以注释掉。
3. **剪映只在 Windows / macOS 有**，Linux 上整条草稿写入环节不成立。
4. **工作流 JSON 和权重不随仓库走** —— ComfyUI 工作流、MiniMax-H3 权重、Real-ESRGAN 权重都是另一套东西，要么自己装，要么把目录一起拷。
5. **项目数据不是必须搬的**，但如果要保留历史项目的中间产物（`align/`、`keyphrases.json`、`manifest.json`），要一起拷——这些是"可以只靠上一段产物重跑下一段"的，丢了就得重新生成（AIGC 那部分是花钱的）。
6. **`config.yaml` 里的路径换机器基本都要改**，`config.example.yaml` 里的每一项都值得对一遍。
