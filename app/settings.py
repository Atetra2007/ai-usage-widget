"""用户设置（settings.json）+ 自动识别数据来源 + 开机自启。"""
import json
import os
import subprocess
import sys
import threading

import paths
from providers import claude as P_claude
from providers import codex as P_codex
from providers import gpu as P_gpu
from providers import local as P_local
from providers.common import NO_WINDOW

SOURCES = ("claude", "codex", "local", "gpu")
STYLES = ("smoke", "frost", "clear")

DEFAULTS = {
    "version": 1,
    "sources": None,            # 第一次运行时按识别结果填
    "seen": [],                 # 用户已经看过的来源（新识别到的来源会在设置里提示）
    "style": "smoke",
    "mode": "popup",            # popup = 悬停展开；pill = 只显示胶囊
    "lang": "auto",             # auto / zh / en
    "claude_refresh": True,     # 访问令牌过期时自动刷新并写回 Claude Code 凭证
    "claude_poll_seconds": 180,
    "warn_percent": 70,
    "danger_percent": 90,
    "snap_distance": 48,
    "snap_margin": 8,
    "hide_on_fullscreen": True,
    "local_log_dirs": [],       # 额外的 llama.cpp 日志目录
    "http_enabled": True,
    "http_port": 8765,
    "http_lan": False,
    "webview_gpu": True,        # WebView2 用 GPU 渲染。关掉（软件渲染）会让透明窗口变成分层窗口，
                                # 导致 Win11 微软拼音候选栏不显示，所以默认开；GPU 进程崩溃由崩溃监听自动重启
    "dev_reload": False,        # 开发用：改代码自动重启
}

# 允许界面修改的键
EDITABLE = {"sources", "style", "mode", "lang", "claude_refresh", "warn_percent", "danger_percent",
            "snap_distance", "snap_margin", "hide_on_fullscreen", "local_log_dirs", "seen"}


class Settings:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "settings.json")
        self.lock = threading.Lock()
        self.first_run = False
        self.data = dict(DEFAULTS)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        else:
            migrated = self._migrate(data_dir)
            det = detect_all(self.data)
            self.data["sources"] = {k: det[k]["status"] == "ok" for k in SOURCES}
            self.data["seen"] = [k for k in SOURCES if det[k]["status"] == "ok"]
            self.first_run = not migrated
            self.save()

    def _migrate(self, data_dir):
        """从早期版本（仓库根目录的 config.json + data/ui.json）迁移。"""
        done = False
        old = os.path.join(paths.ROOT, "config.json")
        if os.path.exists(old):
            try:
                with open(old, "r", encoding="utf-8") as f:
                    c = json.load(f)
                for k in ("claude_poll_seconds", "warn_percent", "danger_percent", "snap_distance",
                          "snap_margin", "hide_on_fullscreen", "http_port", "http_lan"):
                    if k in c:
                        self.data[k] = c[k]
                self.data["dev_reload"] = c.get("dev_reload", True)
                done = True
            except Exception:
                pass
        try:
            with open(os.path.join(data_dir, "ui.json"), "r", encoding="utf-8") as f:
                u = json.load(f)
            if u.get("style") in STYLES:
                self.data["style"] = u["style"]
            if u.get("mode") in ("pill", "popup"):
                self.data["mode"] = u["mode"]
            done = True
        except Exception:
            pass
        return done

    def get(self, k, default=None):
        with self.lock:
            v = self.data.get(k, DEFAULTS.get(k, default))
            return v if v is not None else default

    def update(self, patch, editable_only=True):
        with self.lock:
            for k, v in (patch or {}).items():
                if editable_only and k not in EDITABLE:
                    continue
                if k == "sources" and isinstance(v, dict):
                    cur = dict(self.data.get("sources") or {})
                    cur.update({s: bool(v[s]) for s in SOURCES if s in v})
                    v = cur
                elif k == "style" and v not in STYLES:
                    continue
                elif k == "mode" and v not in ("pill", "popup"):
                    continue
                elif k == "lang" and v not in ("auto", "zh", "en"):
                    continue
                elif k in ("warn_percent", "danger_percent", "snap_distance", "snap_margin"):
                    try:
                        v = max(0, min(100 if "percent" in k else 400, int(v)))
                    except Exception:
                        continue
                self.data[k] = v
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def public(self):
        with self.lock:
            return {k: self.data.get(k) for k in EDITABLE | {"http_port", "http_lan"}}


def detect_all(settings_data=None):
    extra = (settings_data or {}).get("local_log_dirs") or []
    out = {}
    for name, fn in (("claude", lambda: P_claude.detect()), ("codex", lambda: P_codex.detect()),
                     ("local", lambda: P_local.detect(extra)), ("gpu", lambda: P_gpu.detect())):
        try:
            out[name] = fn()
        except Exception:
            out[name] = {"status": "missing"}
    return out


# ---------------------------------------------------------------- 开机自启
LNK_NAMES = ("AI Usage Widget.lnk", "AI 用量小组件.lnk")  # 后者是早期版本的名字


def _startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def autostart_enabled():
    d = _startup_dir()
    return any(os.path.exists(os.path.join(d, n)) for n in LNK_NAMES)


def set_autostart(on):
    d = _startup_dir()
    for n in LNK_NAMES:
        p = os.path.join(d, n)
        if os.path.exists(p):
            os.remove(p)
    if not on:
        return False
    if paths.FROZEN:
        target, args = sys.executable, ""
    else:
        exe = sys.executable
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        target = w if os.path.exists(w) else exe
        args = '"' + os.path.join(paths.APP_DIR, "main.py") + '"'
    env = dict(os.environ, AIW_LNK=os.path.join(d, LNK_NAMES[0]), AIW_TGT=target, AIW_ARGS=args,
               AIW_CWD=paths.ROOT)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:AIW_LNK);"
          "$s.TargetPath=$env:AIW_TGT;$s.Arguments=$env:AIW_ARGS;$s.WorkingDirectory=$env:AIW_CWD;"
          "$s.Description='AI Usage Widget';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                   creationflags=NO_WINDOW, timeout=20, capture_output=True)
    return autostart_enabled()
