# vcp 命令参考

脚本层是 `scripts/vcp/`，用**独立的流水线 venv** 跑（不要用 ComfyUI 的环境——`faster-whisper` 会拖一堆依赖，装进去有搞坏 ComfyUI 的风险）。

```powershell
cd scripts
<venv>\Scripts\python.exe -m vcp --help
```

## 命令清单

| 命令 | 段 | 做什么 |
|---|---|---|
| `vcp init <project-id> --work-root DIR` | — | 建项目目录 + `config.yaml` |
| `vcp check <dir>` | — | 校验 `shots.json` |
| `vcp import-spec <dir> --spec FILE` | — | 把遗留的 `spec_*.json` 迁成 `shots.json` |
| `vcp ui2api <workflow.json> [--out F]` | — | ComfyUI 界面格式工作流 → API 图 |
| `vcp stock <dir> [--only ID...] [--yes]` | 2a | 混剪：检索下载 + 裁切规格化 → `clips/` |
| `vcp footage <dir> [--source F] [--fix-duration]` | 2a' | 口播：实拍入库（保音轨、核对时长） |
| `vcp charset <dir> --image X --name N` | 2b | 角色图 → 四视图人设图 |
| `vcp generate <dir> [--only ID...] [--yes]` | 2b | 镜头表 → 生成 → `clips/`（**默认 dry-run**） |
| `vcp textcheck <dir> [--only ID...]` | — | 检测画面里有没有模型烧进去的文字 |
| `vcp desub <dir> [--only ID...] [--force]` | 2.7 | 抹掉烧进画面的字 → `clips_clean/` |
| `vcp upscale <dir>` | 2.5 | 超分 → `clips_sr/` |
| `vcp align <dir>` | 3 | 抽音轨 + ASR + 听检 → `align/` |
| `vcp keyphrase <dir>` | 3.5 | 重点句 → 解析时间 + 切音频段 |
| `vcp manifest <dir> [--draft-name N]` | 4 | 镜头表 + 对齐 → `manifest.json` |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 正常 |
| `1` | 出错 |
| `2` | **听检发现台词与语音不一致** |

`2` 是**刻意的非零**。它存在的唯一理由就是不让"AI 念错了台词"被静默跳过——排轨逻辑拿到 `2` 就应该停下来问人，而不是继续往下走。

同理，`vcp generate` 检出可疑烧字也**非零退出**。

---

## 关键报告文件

### `generate.json` —— 生成报告

每条镜头一行：`prompt_id` / 实际帧数 / 分辨率 / 耗时 / 字节数 / seed，用于回查某一条是怎么来的。

产物落两处是**有意的**：

```
ComfyUI output/vcp_gen/<id>_00001_.mp4  →  gen/<id>_00001_.mp4   原始产物，保留
                                        →  clips/<id>.mp4        后续工序用
```

先落 `gen/` 再复制成 `clips/`：规格化或超分把 `clips/` 里的搞坏了还能回退到 `gen/`。

### 帧数必须核对，但**量化不是失败**

生成器把实测帧数与请求的 `total_frames` 比对，分三档：

- **对得上**：±1 帧内（探针按毫秒报时长本身就有半帧误差）
- **量化**：模型把帧数抬到了上一档。这是 MiniMax-H3 的固定行为，档位等差 17 帧：

  ```
  124 / 141 / 158 / 175 / 192 / 209 / 226 / 243
  请求值一律抬到不小于它的那一档
  ```

  记 **warning**，并按实测把 `shots.json` 的 `duration` 改正——口径和 `vcp footage` 一致：视频说了算，镜头表反过来对齐它。
- **异常**：比请求短（被截断）、或跨了一档以上。这档才记 error。

为什么量化也必须处理：排轨用的是 `shots.json` 的时长，视频实际长度对不上会让整条时间轴错位，表现是"后面所有镜头都对不齐"，很难反查。

> ⚠️ **量化绝不能记成 error。** 记成 error 会连带把烧字重试循环短路掉（`if r.get("error") or r.get("skipped") or not suspect: break`），实测开了 `retry_on_text: 2` 却一次都没重试过。

### `upscale.json` / `desub.json`

- `upscale.json`：每条的分辨率变化、耗时、来源文件
- `desub.json`：mask 坐标、前后分数、前后白像素、帧数变化

**超分必须断言输出分辨率真的等于目标。** 如果日志显示 `480x864 -> 480x864`，说明挑到了输入文件而不是产物。

**去烧字必须复检**：带子里的白像素要归零，否则算失败。复检不过就把产物删掉——留着会被 `resolved_clip` 选中，等于把坏产物当好产物用。

判定用 **score 而不是白像素**：score 是标定过的（干净画面 0.012~0.021，烧字 0.147~0.148）。白像素数只当诊断，它会被带子里本来就亮的东西带偏——实测某项目一条片段里是一只手端着白纸杯，score 只有 0.1355（不算烧字）却有 1278 个白像素，一个字都没有。拿白像素当硬判定会把好产物判死。

### `align/<shot-id>.json` 与 `keyphrases.json`

- `align/`：ASR 逐字时间戳 + 与 `shots.json` 里 `text` 的比对结果（覆盖率、不一致率）
- `keyphrases.json`：重点句清单，含独立动画配置、音效配置、对应音频段

---

## 时间轴：段必须首尾严格相接

视频段之间**不能有 1 毫秒的重叠或空隙**。

做法：把这一段的两个端点各自吸附到毫秒，再用端点差算时长。

为什么不能分别四舍五入 `start` 和 `duration`：`cursor` 是精确浮点累加的，而 `start` / `duration` 各自 rounding 到毫秒时误差方向不同，就会出现「上一段的 end 比下一段的 start 大 1 毫秒」。

剪映的 `build_draft` 对重叠是**零容忍**的（实测报 `SegmentOverlap`：某项目 c9 渲染成 `[63.292, 70.584]`，而 c10 精确起点是 `70.583`）。

用端点差算时长之后，段 i 的 `end` 和段 i+1 的 `start` 是同一个整数，**从构造上就不可能重叠**。

---

## 草稿名要加后缀

```
vcp manifest <dir> --draft-name "项目名_v2"
```

草稿是按名字定位并**整目录重建**的，同名会把原来那份冲掉。有手工改过的草稿一定要加后缀。

生成后再通过剪映 MCP 的 `build_draft` 落草稿，**先 `dry_run: true` 校验，通过再传 `false`**。
