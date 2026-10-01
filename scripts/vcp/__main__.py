"""`python -m vcp` 的入口。

Windows 控制台默认不是 UTF-8，中文输出会乱码，所以这里先把两个流强制成 UTF-8。
"""

import sys


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8()

from .cli import main  # noqa: E402  （必须在流调整之后导入）

sys.exit(main())
