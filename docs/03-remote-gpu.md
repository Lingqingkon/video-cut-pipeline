# 远端 GPU

生成、超分、去烧字三件事都在远端跑。本地只负责编排、素材处理和剪映草稿。

## 为什么放远端

生视频是唯一真正吃显卡的环节。本地机器的显卡要同时伺候日常开发和预览，跑一条几分钟的生成就被占满了。远端实例按小时计费，用完就关，比配一张大卡划算。

实际使用的实例规格：

| 项 | 值 |
|---|---|
| GPU | RTX 5090 / 32GB（驱动 595.x，compute capability 12.0） |
| CPU / 内存 | 208 核 / 754GB |
| 数据盘 | `/root/autodl-tmp` 240GB |
| Python | miniconda3 的 3.12，torch 2.8.0+cu128 |

---

## 盒子上装了什么

| 组件 | 位置 / 版本 |
|---|---|
| ComfyUI | `/root/autodl-tmp/ComfyUI`（0.35.0） |
| custom_nodes | KJNodes、VideoHelperSuite、Comfyroll、MiniMaxH3_Director、rgthree |
| MiniMax-H3 权重 | fl2va / ref2va int8 各约 20.9GB |
| 文本编码器 | qwen3vl 32B（nvfp4/awq 量化） |
| VAE | video VAE fp16 + audio VAE fp32 |
| 超分权重 | `RealESRGAN_x4plus.pth`（放在 `models/upscale_models/`） |
| 工作流 | Director.json、MiniMax+H3 全能参考生视频（动作迁移）.json |
| 去烧字 | `/root/autodl-tmp/vsr/video-subtitle-remover`（独立 venv） |

---

## 每次开机都要手动起 ComfyUI

实例关机后 ComfyUI 是停的。启动命令：

```bash
cd /root/autodl-tmp/ComfyUI
setsid nohup /root/miniconda3/bin/python main.py --listen 0.0.0.0 --port 6006 \
  > /root/autodl-tmp/logs/comfyui_6006.log 2>&1 < /dev/null &
```

### `< /dev/null` 不能省

只重定向 stdout/stderr 的话，后台进程**仍然持有 stdin**，远端 channel 永不关闭，脚本读输出会一直超时。这个坑的现象是"命令发出去了、日志也写了、但读不回来"，很容易误判成网络问题。

---

## 端口与隧道

| 项 | 值 |
|---|---|
| 盒子内 ComfyUI 监听 | `0.0.0.0:6006` |
| 本地隧道口 | `6006` |
| 文件传输 | 远程模式下片段走 SFTP 到盒子的 `input/`，产物再 SFTP 下回来 |

**为什么不直连盒子的公网端口**：云实例的自定义服务端口配置不稳定，而且 SSH 隧道换机器不用改盒子配置。

隧道由命令**自己开关**（基于 paramiko），不依赖外部 `ssh -L`，也不需要在盒子上常驻任何转发进程。`vcp` 在需要时建立隧道，跑完关掉。

---

## 去烧字（VSR）

装在盒子上，不在本地。位置和解释器都在配置里：

```yaml
desub:
  remote_root: "/root/autodl-tmp/vsr"
  repo:        "/root/autodl-tmp/vsr/video-subtitle-remover"
  python:      "/root/autodl-tmp/vsr/venv/bin/python"
  mode: "sttn-det"
```

**必须用 `sttn-det`。** `sttn-auto` 那个模型的输入是 3 通道、没有 mask，指望它自己"学会"擦字——实测同一块区域文字笔画像素 1589 → 1589，等于没做。

音频用 `-codec copy` 原样合回，所以时间基准不变。

---

## 三个会踩的运维坑

### 1. 地址每次开机都会变

云实例关机后原地址立即失效，开机后 host 和 port 都会变。**连不上先怀疑这个**，不要去改代码。

配套的两个设计：
- 凭据不写在项目配置里，走环境变量或独立的 secrets 文件
- 隧道在命令内部按需建立，所以换地址只需要改一处配置

### 2. 盒子上没有 ffmpeg

PATH 里没有。视频编码靠 ComfyUI 自己，**不要指望在盒子上调 ffmpeg 做混流**。所有 ffmpeg 相关的工作（规格化、抽音轨、抽帧、合回音轨）都在本地做，需要传输的素材走 SFTP。

### 3. 网络受限

- **GitHub / HuggingFace 直连不通**（实测 HTTP 000）
- `hf-mirror.com` 通，下模型走它
- 下 GitHub 上的东西前先执行 `source /etc/network_turbo`

下大文件（模型权重动辄十几 GB）必须**核对字节数**。`curl` 中断会留下半截文件，而半截的 `.pth` / `.safetensors` 只在加载时才报错，且报错信息看不懂。正确做法是写成"断点续传 + 无限重试 + 字节精确校验"的循环，不要用一条 `curl` 了事。

---

## 速度

| 操作 | 耗时 |
|---|---|
| 生成 124 帧（5.17 秒，480×864） | 约 71 秒 |
| 生成 158 帧（6.58 秒） | 约 90 秒 |
| 超分 480×864 → 1080×1920（124 帧） | 约 126 秒（约 24 倍实时） |

一个 7 镜头的项目，生成环节约 10 分钟 GPU。这就是为什么镜头表必须先人工确认再放行。
