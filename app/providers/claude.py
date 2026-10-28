"""Claude：额度来自 Claude Code 登录凭证 + 非公开的 oauth/usage 接口；token 来自 Claude Code 本地日志。"""
import glob
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

from .common import FileCache, History, local_date, now, parse_iso, today, window_info  # noqa: F401

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URLS = ["https://platform.claude.com/v1/oauth/token",
                     "https://console.anthropic.com/v1/oauth/token"]
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def default_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def detect(claude_dir=None):
    """ok = 已登录 Claude Code；no_login = 装了但没登录；missing = 没装。"""
    d = claude_dir or default_dir()
    if os.path.exists(os.path.join(d, ".credentials.json")):
        return {"status": "ok", "path": d}
    if os.path.isdir(d):
        return {"status": "no_login", "path": d}
    return {"status": "missing", "path": d}


class TokenExpired(Exception):
    pass


class ClaudeCollector:
    def __init__(self, claude_dir, cache, history, data_dir, poll_seconds=180, allow_refresh=True):
        self.allow_refresh = allow_refresh
        self.dir = claude_dir
        self.cred_path = os.path.join(claude_dir, ".credentials.json")
        self.cache = cache
        self.history = history
        self.data_dir = data_dir
        self.poll = max(60, int(poll_seconds))
        self.next_poll = 0
        self.backoff = self.poll
        self.usage = None
        self.usage_ts = None
        self.error = None
        self.state = {"ok": False}
        try:  # 重启后先显示上一次的额度，不用等第一次查询
            last = os.path.join(data_dir, "claude_last_response.json")
            if now() - os.path.getmtime(last) < 3600:
                with open(last, "r", encoding="utf-8") as f:
                    self.usage = self._parse_usage(json.load(f))
                self.usage_ts = os.path.getmtime(last)
        except Exception:
            pass

    # ---- 令牌
    def _read_creds(self):
        with open(self.cred_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _refresh(self, creds):
        oauth = creds["claudeAiOauth"]
        body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                           "client_id": CLAUDE_CLIENT_ID}).encode()
        last_err = None
        for url in CLAUDE_TOKEN_URLS:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "ai-usage-widget/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    resp = json.loads(r.read().decode())
                break
            except Exception as e:  # noqa
                last_err = e
                resp = None
        if not resp:
            raise RuntimeError(f"refresh_failed: {last_err}")
        # 写回前先读一次最新文件（Claude Code 可能刚刷新过），再备份
        try:
            fresh = self._read_creds()
            if fresh["claudeAiOauth"].get("accessToken") != oauth.get("accessToken"):
                return fresh  # 别人已经刷新过了
        except Exception:
            fresh = creds
        try:
            with open(self.cred_path + ".bak", "w", encoding="utf-8") as f:
                json.dump(fresh, f)
        except Exception:
            pass
        o = fresh["claudeAiOauth"]
        o["accessToken"] = resp["access_token"]
        if resp.get("refresh_token"):
            o["refreshToken"] = resp["refresh_token"]
        o["expiresAt"] = int((now() + float(resp.get("expires_in", 3600))) * 1000)
        tmp = self.cred_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(fresh, f)
        os.replace(tmp, self.cred_path)
        return fresh

    def _token(self, force=False):
        creds = self._read_creds()
        o = creds["claudeAiOauth"]
        exp = (o.get("expiresAt") or 0) / 1000
        if force or exp - now() < 300:
            if not self.allow_refresh:
                if exp > now():
                    return o["accessToken"], o.get("subscriptionType")
                raise TokenExpired()
            creds = self._refresh(creds)
        return creds["claudeAiOauth"]["accessToken"], creds["claudeAiOauth"].get("subscriptionType")

    def _fetch_usage(self, retry=True):
        token, plan = self._token()
        req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json", "User-Agent": "ai-usage-widget/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and retry:
                self._token(force=True)
                return self._fetch_usage(retry=False)
            raise
        try:
            with open(os.path.join(self.data_dir, "claude_last_response.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return data, plan

    @staticmethod
    def _pct(v):
        if v is None:
            return None
        v = float(v)
        return v  # 接口返回 0-100 的百分比（首次运行后用 claude_last_response.json 核对）

    def _parse_usage(self, data):
        out = {}
        for key, name, mins in (("five_hour", "five_hour", 300), ("seven_day", "weekly", 10080)):
            w = data.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                out[name] = (self._pct(w.get("utilization")), parse_iso(w.get("resets_at")), mins)
        for lim in data.get("limits") or []:
            kind = str(lim.get("kind", "")).lower()
            if lim.get("scope"):
                continue
            pct = lim.get("percent", lim.get("utilization"))
            if pct is None:
                continue
            if ("5h" in kind or "five" in kind or "session" in kind) and "five_hour" not in out:
                out["five_hour"] = (float(pct), parse_iso(lim.get("resets_at")), 300)
            elif ("7d" in kind or "seven" in kind or "week" in kind) and "weekly" not in out:
                out["weekly"] = (float(pct), parse_iso(lim.get("resets_at")), 10080)
        return out

    def poll_usage(self):
        if now() < self.next_poll:
            return
        if not os.path.exists(self.cred_path):
            self.error = "not_logged_in"
            self.next_poll = now() + 30
            return
        try:
            data, plan = self._fetch_usage()
            self.usage = self._parse_usage(data)
            self.plan = plan
            self.usage_ts = now()
            self.error = None
            self.backoff = self.poll
            self.next_poll = now() + self.poll
        except urllib.error.HTTPError as e:
            self.error = f"http_{e.code}"
            self.backoff = min(900, self.backoff * 2) if e.code == 429 else self.poll
            self.next_poll = now() + self.backoff
        except TokenExpired:
            self.error = "token_expired"
            self.next_poll = now() + 60
        except Exception as e:  # noqa
            self.error = "refresh_failed" if "refresh_failed" in str(e) else "network"
            self.next_poll = now() + self.poll

    # ---- Claude Code 本地 token
    @staticmethod
    def _parse_project_file(path):
        rows = {}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                key = f'{msg.get("id")}:{d.get("requestId")}'
                ts = parse_iso(d.get("timestamp")) or 0
                inp = int(u.get("input_tokens", 0) or 0)
                cw = int(u.get("cache_creation_input_tokens", 0) or 0)
                cr = int(u.get("cache_read_input_tokens", 0) or 0)
                out = int(u.get("output_tokens", 0) or 0)
                rows[key] = [local_date(ts) if ts else today(), inp + cw + cr + out, inp + cw + out]
        return rows

    def scan_tokens(self):
        files = glob.glob(os.path.join(self.dir, "projects", "**", "*.jsonl"), recursive=True)
        seen = {}
        for fp in files:
            try:
                st = os.stat(fp)
            except OSError:
                continue
            v = self.cache.get("claude:" + fp, st)
            if v is None:
                try:
                    v = self._parse_project_file(fp)
                except Exception:
                    continue
                self.cache.put("claude:" + fp, st, v)
            seen.update(v)
        daily = defaultdict(lambda: {"total": 0, "fresh": 0})
        for day, tot, fresh in seen.values():
            daily[day]["total"] += tot
            daily[day]["fresh"] += fresh
        self.history.merge("claude", daily)

    def build(self):
        td, tt = self.history.totals("claude")
        st = {"ok": self.usage is not None, "logged_in": os.path.exists(self.cred_path),
              "error": self.error, "today": td, "total": tt, "updated": self.usage_ts,
              "five_hour": None, "weekly": None, "week": self.history.last_days("claude")}
        if self.usage:
            for name in ("five_hour", "weekly"):
                if name in self.usage:
                    st[name] = window_info(*self.usage[name])
        self.state = st
        return st
