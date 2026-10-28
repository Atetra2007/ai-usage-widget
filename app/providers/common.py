"""各数据来源共用的小工具：时间、额度窗口、文件缓存、每日用量历史。"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0


def now():
    return time.time()


def local_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def parse_iso(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return float(s) / (1000 if s > 1e12 else 1)
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def window_info(used, resets_at, window_minutes):
    """统一的额度窗口：用量、重置时间、按时间均匀消耗的位置、预计用完时间。"""
    if used is None:
        return None
    t = now()
    win = (window_minutes or 0) * 60
    reset_passed = bool(resets_at and resets_at <= t)
    if reset_passed:
        used = 0.0
    used = max(0.0, min(100.0, float(used)))
    info = {"used": round(used, 1), "resets_at": resets_at, "window_minutes": window_minutes,
            "reset_passed": reset_passed, "pace": None, "eta": None, "ahead": False}
    if resets_at and win and not reset_passed:
        remaining = resets_at - t
        elapsed = max(0.0, win - remaining)
        pace = min(100.0, elapsed / win * 100)
        info["pace"] = round(pace, 1)
        if elapsed > 60 and used > 0:
            rate = used / elapsed  # 百分比/秒
            eta = t + (100 - used) / rate
            info["eta"] = eta if eta < resets_at else None
            info["ahead"] = used > pace + 5
    return info


class FileCache:
    """按文件 size/mtime 缓存解析结果，避免反复全量读取。"""

    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, key, st):
        e = self.data.get(key)
        if e and e.get("size") == st.st_size and e.get("mtime") == int(st.st_mtime):
            return e.get("v")
        return None

    def put(self, key, st, v):
        self.data[key] = {"size": st.st_size, "mtime": int(st.st_mtime), "v": v}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


class History:
    """每日用量持久化：日志被清理后历史也不会丢（同一天取较大值）。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def merge(self, source, daily):
        with self.lock:
            d = self.data.setdefault(source, {})
            changed = False
            for day, vals in daily.items():
                old = d.get(day, {})
                new = {k: max(int(v), int(old.get(k, 0))) for k, v in vals.items()}
                if new != old:
                    d[day] = new
                    changed = True
            if changed:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)

    def totals(self, source, field="total"):
        with self.lock:
            d = self.data.get(source, {})
            return int(d.get(today(), {}).get(field, 0)), int(sum(v.get(field, 0) for v in d.values()))

    def last_days(self, source, n=7, field="total"):
        from datetime import timedelta
        with self.lock:
            d = self.data.get(source, {})
            out = []
            for i in range(n - 1, -1, -1):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                out.append([day, int(d.get(day, {}).get(field, 0))])
            return out
