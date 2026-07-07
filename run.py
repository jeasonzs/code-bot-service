#!/usr/bin/env python3
"""Dev runner: 跳过 pip install, 直接 python run.py ...

用法:
    python run.py --sim                    # sim 模式 (推荐, 无需设备)
    python run.py --sim --sim-port 9000
    python run.py start -v                 # 真 USB, verbose
    python run.py test-protocol            # codec 自测

把 src/ 加到 sys.path, 避免装包就能 import codebot.
"""

import sys
from pathlib import Path

# 把 src/ 加到 import path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

# 转发到 click CLI (跟 `codebotd` 命令一样, 但不依赖 entry_points)
from codebot.cli import cli

if __name__ == "__main__":
    cli()