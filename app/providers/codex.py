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
        self.history.merge("codex", merged)
        td, tt = self.history.totals("codex")
        st = {"ok": bool(files), "today": td, "total": tt, "five_hour": None, "weekly": None,
              "updated": latest[0] if latest else None, "week": self.history.last_days("codex")}
        if latest:
            r = latest[1]
            for key, name in (("primary", "five_hour"), ("secondary", "weekly")):
                w = r.get(key)
                if w:
                    st[name] = window_info(w.get("used_percent"), w.get("resets_at"), w.get("window_minutes"))
        self.state = st
        return st
