"""路径：源码运行 / 打包成 exe 两种情况下，程序资源和用户数据各放哪里。"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)          # ui/ 所在目录
ROOT = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(APP_DIR)
UI_HTML = os.path.join(RES_DIR, "ui", "index.html")


def data_dir():
    """设置和历史用量放哪：
    - 环境变量 AIW_DATA 优先；
    - exe 版默认 %LOCALAPPDATA%\\AIUsageWidget（升级覆盖程序不丢数据），
      exe 旁边放一个 portable.txt 就改为便携模式，数据放在 exe 旁的 data\\；
    - 源码运行放在仓库的 data\\（已被 .gitignore 忽略）。"""
    d = os.environ.get("AIW_DATA")
    if not d:
        if FROZEN and not os.path.exists(os.path.join(ROOT, "portable.txt")):
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            d = os.path.join(base, "AIUsageWidget")
        else:
            d = os.path.join(ROOT, "data")
    os.makedirs(d, exist_ok=True)
    return d
