"""显卡：nvidia-smi。"""
import os
import shutil
import subprocess

from .common import NO_WINDOW


def _exe():
    p = shutil.which("nvidia-smi")
    if p:
        return p
    p = r"C:\Windows\System32\nvidia-smi.exe"
    return p if os.path.exists(p) else None


def detect():
    return {"status": "ok" if _exe() else "missing"}


def read_gpu():
    try:
        out = subprocess.run(
            [_exe() or "nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW).stdout.strip()
        used, total, util, name = [x.strip() for x in out.splitlines()[0].split(",", 3)]
        return {"used": round(int(used) / 1024, 1), "total": round(int(total) / 1024, 1),
                "util": int(util), "name": name}
    except Exception:
        return None
