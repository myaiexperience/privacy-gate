#!/usr/bin/env python3
"""
仓库根目录的入口 —— 转发到 tools/cli.py

它的作用只有一个：让"clone 下来就能跑"成立。

    python privacy_gate.py classify --stdin
    python privacy_gate.py doctor

装好之后请用 console script（`privacy-gate ...`）或 `python -m privacy_gate ...`。
实现都在 `tools/cli.py`，这里不重复一份——两份实现迟早会漂移。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
