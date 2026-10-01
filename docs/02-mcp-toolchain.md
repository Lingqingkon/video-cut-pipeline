# MCP 工具链

## 为什么是 MCP

这套流水线要和一堆**桌面软件**打交道：剪映（写草稿）、Photoshop（合成封面）、Blender（改场景）。它们都没有好用的命令行接口，但都有可编程通道——剪映的草稿是 JSON、PS 有 COM + ExtendScript、Blender 有 Python API。

MCP（Model Context Protocol）在这里的作用是**把这些通道统一成模型能调的工具**：每个服务是一个独立进程，声明若干工具，模型按名字调用。好处是模型不需要知道 PS 的 COM 怎么起、剪映的草稿文件在哪，只需要知道"有个工具叫 `photoshop_place_image`"。

这些服务是独立进程，通过 stdio 或 streamable-http 与 Agent 通信，配置写在 Agent 的 composition 里。

---

## 五个服务

| 服务 | 传输 | 干什么 | 工具数 |
|---|---|---|---|
| **剪映**（jianying） | stdio | 生成 / 改写剪映草稿文件（`draft_content.json`），不驱动界面 | 8 |
| **Blender**（blender） | stdio | 驱动当前**打开着**的 Blender 会话，改真实场景 | — |
| **读图**（image） | stdio | 把本地图片 / 视频帧真正作为图像返回给模型 | 3 |
| **Photoshop**（photoshop） | stdio | COM + ExtendScript 驱动本机 PS | 119 |
| **Unity**（unity） | streamable-http | 连接 Unity / 团结引擎工程 | — |

---

## 剪映 MCP —— 写草稿的唯一通道

这是整条链路的出口，也是**唯一**能写剪映草稿的工具。

> 生产路径上写草稿只能走 `build_draft`，没有独立的多轨写草稿脚本。

### 8 个工具

```
get_jianying_capabilities      看检测到的剪映版本与能力
analyze_video_source           抽帧 + 语句级字幕，供模型"看片"
save_video_observations        把模型对画面的理解存回去
get_video_analysis_context     分页读取已缓存的分析结果
analyze_reference_draft        读参考草稿，提取剪辑蓝图
build_draft                    ★ 主入口：提交编辑清单生成新草稿
build_from_reference           复制参考草稿的蓝图并替换素材/文本
batch_from_template            基于明文模板批量生成草稿
```

### 几个必须知道的点

**草稿是按名字定位并整目录重建的。** 同名会把你原来那份冲掉。要保留手工草稿，生成时用 `--draft-name` 加后缀（如 `_v2`）。

**`build_draft` 先 dry-run 再发布。** 默认 `dry_run: true` 只在暂存区完整构建和校验，确认后才传 `false` 发布到剪映草稿目录。

**它不负责导出。** 流程终点是草稿工程，最后一步导出由人在剪映里点。这条在交付说明里必须写清楚。

**版本敏感。** MCP 只在剪映 `10.0.5` 上验证过。更新的版本上，动画、在线资源、滤镜、草稿格式都可能失效或显示不同——改这些相关的东西之后要肉眼确认一次。

**剪映会自动升级并删除旧版本目录**，所以不要写死可执行文件路径，交给服务自己探测。

### 环境变量

```
JIANYING_DRAFT_ROOT     剪映草稿根目录（build_draft 的发布目标）
JIANYING_MCP_WORK_ROOT  服务的工作目录
JIANYING_EXE            剪映可执行文件（不写死，交服务探测）
JIANYING_FFMPEG         ffmpeg 路径（抽帧与字幕能力依赖）
JIANYING_FFPROBE        ffprobe 路径
JIANYING_DRAFT_CRYPTO   加密草稿的解密配置（可选）
```

---

## 读图 MCP —— 补上"模型看不见图"这块

剪映 MCP 的 `analyze_video_source` 只回**文件路径**，不回图像本身。模型拿到一堆路径是没法判断画面内容的。

读图 MCP 就做一件事：把本地图片和视频帧**真正作为图像返回**给模型。3 个工具：

```
view_image         看一张本地图片
view_image_crop    裁剪放大某个区域（看人脸、手、细节）
media_info         探测视频/音频的时长、分辨率、编码、帧率
```

配套还有按时间范围抽帧、生成接触印相（contact sheet）的能力——长视频不能一次抽几百帧塞进上下文，要按候选区间取。

> 这个服务看起来很小，但**它是"素材筛选"和"验收"两个环节的基础**。没有它，素材里的人是亚洲人还是欧美人、字幕有没有叠、画面有没有烧字，模型全都只能靠猜。

---

## Photoshop MCP —— 封面与静态合成

通过 **COM + ExtendScript** 驱动本机 Photoshop（走 `Photoshop.Application`，本机实测附着约 0.006 秒）。用来做 B 站封面这类需要排版和抠图的任务：置入底图、置入抠出来的主体、建中文文字层、设字体、导出。

- 共 119 个工具，覆盖图层、选区、调整层、滤镜、智能对象、批处理、数据驱动图形等。
- 前置条件：Photoshop 必须处于**运行状态**（COM 附着，不是启动它）。

**一个已知坑**：上游 issue #40——Windows 下 `cscript` 的 stdout 是 ANSI 编码，中文文本"读回"会乱码。写入路径未证实受影响，但**验收一律靠截图肉眼确认，不靠读回文本**。

---

## Blender MCP —— 驱动你正开着的那个会话

和"打开文件、改、保存"不同，这个服务连接的是**你当前打开着的 Blender 实例**，改的是真实场景，改完你在视口里立刻能看到。

实现是一个装在 Blender 里的插件（addon）在本机起一个桥接服务，MCP 服务连上去。端口必须与 Blender 侧面板显示的一致（默认 `26177`）。

能力包括：执行 Python、读场景结构、改对象与材质、视口截图（用 GPU 离屏渲染抓图，所以即使 Blender 窗口不在前台也不会全黑）。

---

## Unity MCP —— 唯一走 HTTP 的服务

通过 streamable-http 连接本机固定端口（`26176`），由 Unity 侧的插件起服务，两边都连它。

**它没有"初次连接失败"的自动重连。** 如果 Agent 启动时那个端口还没在监听（比如 Unity 后开），工具不会注册；而 supervisor 只在**已建立的连接断开**时才重连。

补救办法：改动这条配置即触发 patch 热重载，连接就地重建，不需要重启 Agent。

---

## 配置长什么样

以剪映 MCP 为例（路径按你的实际安装位置替换）：

```yaml
- insert:
    - id: mcp-jianying
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: jianying
        transport: stdio
        command: '<venv>\Scripts\python.exe'
        args: ['-m', 'jianying_ai_mcp']
        cwd: '<repo>\jianying-ai-mcp'
        toolCallTimeoutMs: 300000
        env:
          JIANYING_DRAFT_ROOT: '<剪映草稿目录>'
          JIANYING_MCP_WORK_ROOT: '<work>'
          JIANYING_FFMPEG: '<ffmpeg.exe>'
          JIANYING_FFPROBE: '<ffprobe.exe>'
```

超时统一给得比较大（剪映 / PS / Blender 都是 300000 ms）——这些工具背后的操作是渲染、编码、跑脚本，不是查询。
