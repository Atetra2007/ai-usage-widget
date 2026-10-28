"""Codex：读 ~/.codex/sessions 下的会话日志（token 用量 + 5 小时/每周额度）。"""
import glob
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

from .common import FileCache, History, local_date, now, parse_iso, today, window_info  # noqa: F401

def default_dir():
    return os.path.join(os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"), "sessions")


def detect(sessions_dir=None):
    d = sessions_dir or default_dir()
    if glob.glob(os.path.join(d, "**", "rollout-*.jsonl"), recursive=True):
        return {"status": "ok", "path": d}
    if os.path.isdir(os.path.dirname(d)):
        return {"status": "no_sessions", "path": d}
    return {"status": "missing", "path": d}


class CodexCollector:
    def __init__(self, sessions_dir, cache, history):
        self.dir = sessions_dir
        self.cache = cache
        self.history = history
        self.state = {"ok": False}

    def _parse(self, path):
        daily = defaultdict(lambda: {"total": 0, "fresh": 0})
        prev = None
        rl = None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"token_count"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "token_count":
                    continue
                ts = parse_iso(d.get("timestamp")) or 0
                info = p.get("info") or {}
                tot = info.get("total_token_usage")
                if tot:
                    cur = (int(tot.get("total_tokens", 0)),
                           int(tot.get("input_tokens", 0)) - int(tot.get("cached_input_tokens", 0))
                           + int(tot.get("output_tokens", 0)))
                    if prev is None or cur[0] < prev[0]:
                        delta = cur
                    else:
                        delta = (cur[0] - prev[0], max(0, cur[1] - prev[1]))
                    prev = cur
                    day = local_date(ts) if ts else today()
                    daily[day]["total"] += delta[0]
                    daily[day]["fresh"] += delta[1]
                r = p.get("rate_limits") or {}
                if r.get("primary") or r.get("secondary"):
                    rl = [ts, r]
        return {"daily": dict(daily), "rl": rl}

    def scan(self):
        files = glob.glob(os.path.join(self.dir, "**", "rollout-*.jsonl"), recursive=True)
        merged = defaultdict(lambda: {"total": 0, "fresh": 0})
        latest = None
        for fp in files:
            try:
                st = os.stat(fp)
            except OSError:
                continue
            v = self.cache.get("codex:" + fp, st)
            if v is None:
                try:
                    v = self._parse(fp)
                except Exception:
                    continue
                self.cache.put("codex:" + fp, st, v)
            for day, vals in v["daily"].items():
                merged[day]["total"] += vals["total"]
                merged[day]["fresh"] += vals["fresh"]
            if v.get("rl") and (latest is None or v["rl"][0] > latest[0]):
                latest = v["rl"]
        self.cache.prune("codex:", {"codex:" + fp for fp in files})
        self.history.merge("codex", merged)
        td, tt = self.history.totals("codex")
        st = {"ok": bool(files), "today": td, "total": tt, "five_hour": None, "weekly": None,
              "updated": latest[0] if latest else None, "week": self.history.last_days("codex")}
        if latest:
            ts, r = latest
            for key in ("primary", "secondary"):
                w = r.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                mins = w.get("window_minutes")
                # 按窗口长度归类：只有每周额度的套餐里，primary 就是 7 天窗口
                if mins:
                    name = "weekly" if mins >= 1440 else "five_hour"
                else:
                    name = "five_hour" if key == "primary" else "weekly"
                resets = parse_iso(w.get("resets_at"))
                if resets is None and w.get("resets_in_seconds") is not None:  # 旧版 Codex 的格式
                    resets = ts + float(w["resets_in_seconds"])
                if st[name] is None:
                    st[name] = window_info(w.get("used_percent"), resets, mins or (300 if name == "five_hour" else 10080))
        self.state = st
        return st
