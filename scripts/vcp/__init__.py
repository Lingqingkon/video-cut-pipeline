"""vcp —— video-cut-pipeline 的脚本层。

分工原则：
  - 确定性的事（抽音轨、ASR、算时间轴、拼 manifest、校验）放脚本里
  - 创意的事（提取元素、设计镜头、写 prompt）由模型按 SKILL.md 的规则做

产物契约见 ../references/artifacts.md，不要在这里另造数据结构。
"""

__version__ = "0.1.0"
