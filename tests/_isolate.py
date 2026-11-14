"""隔离模式引导：python -I tests/_isolate.py -m biodiversity_archive ...

-I 会忽略 PYTHONPATH，因此由本脚本显式注入项目根目录，再把命令行转交给
biodiversity_archive 的 main()。脚本在项目目录内，资料包路径仍作为参数传入，
解释器不会从不信任的资料目录加载代码。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from biodiversity_archive.__main__ import main  # noqa: E402

if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '-m':
        sys.argv = ['biodiversity_archive', *sys.argv[2:]]
    main()
