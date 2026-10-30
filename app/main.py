"""AI Usage Widget：Claude / Codex / 本地模型用量的桌面悬浮小组件（Windows）。

结构：后台采集线程（providers/*） -> 状态快照 -> pywebview 置顶透明小窗（ui/index.html）
      另有一个本机 JSON 接口 http://127.0.0.1:8765/status.json 可供其他设备读取。
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import paths  # noqa: E402
import settings as S  # noqa: E402
from providers import claude as P_claude  # noqa: E402
from providers import codex as P_codex  # noqa: E402
from providers import codex_switch as P_switch  # noqa: E402
from providers import gpu as P_gpu  # noqa: E402
from providers import local as P_local  # noqa: E402
from providers import lmctl as P_lmctl  # noqa: E402
from providers.common import FileCache, History  # noqa: E402

VERSION = "1.0.3"
TITLE = "AI Usage Widget"
IS_WIN = sys.platform == "win32"
DATA = paths.data_dir()


def log_debug(msg):
    try:
        p = os.path.join(DATA, "debug.log")
        if os.path.exists(p) and os.path.getsize(p) > 512 * 1024:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def log_error(where):
    try:
        p = os.path.join(DATA, "error.log")
        if os.path.exists(p) and os.path.getsize(p) > 512 * 1024:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{traceback.format_exc()}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 采集
class Engine:
    def __init__(self, st):
        self.st = st
        self.cache = FileCache(os.path.join(DATA, "cache.json"))
        self.history = History(os.path.join(DATA, "history.json"))
        self.claude = P_claude.ClaudeCollector(P_claude.default_dir(), self.cache, self.history, DATA,
                                               st.get("claude_poll_seconds", 180), st.get("claude_refresh", True))
        self.switch = P_switch.CodexSwitcher(P_codex.codex_home(), os.path.join(DATA, "codex_vault"))
        self.codex = P_codex.CodexCollector(P_codex.default_dir(), self.cache, self.history, DATA, self._codex_opts)
        self._local_dirs = None
        self.local = None
        self._make_local()
        self.gpu = None
        self.lmctl = P_lmctl.LMControl()  # 本地模型手动加载 / 卸载
        self.detect = S.detect_all(st.data)
        self.lock = threading.Lock()
        self.state = {}
        self.stop = threading.Event()
        self._slots_logged = None

    def _make_local(self):
        dirs = [d for _, d in P_local.default_dirs()] + list(self.st.get("local_log_dirs") or [])
        if dirs != self._local_dirs:
            self._local_dirs = dirs
            self.local = P_local.LocalCollector(dirs, self.cache, self.history)

    def on(self, k):
        return bool((self.st.get("sources") or {}).get(k))

    def _loop(self, name, interval, fn):
        while not self.stop.is_set():
            try:
                fn()
            except Exception:
                log_error(name)
            self.stop.wait(interval)

    def _slow(self):
        if self.on("codex"):
            self.codex.scan()
        if self.on("claude"):
            self.claude.scan_tokens()
        self.cache.save()

    def _codex_opts(self):
        return {"live": bool(self.st.get("codex_live", True)),
                "others": bool(self.st.get("codex_other_accounts", True)),
                "extra": self.st.get("codex_auth_files") or [],
                "poll": self.st.get("codex_poll_seconds", 180),
                "labels": self.st.get("codex_labels") or {},
                "vault": self.switch.identities if self.st.get("codex_switch") else None}

    def _codex_api(self):
        if self.on("codex"):
            self.codex.poll()
            if self.st.get("codex_switch") and self.st.get("codex_live", True):
                self.switch.refresh_names()

    def _claude(self):
        if self.on("claude"):
            self.claude.allow_refresh = bool(self.st.get("claude_refresh", True))
            self.claude.poll_usage()

    def _gpu(self):
        self.gpu = P_gpu.read_gpu() if (self.on("local") and self.on("gpu")) else None

    def _lm(self):
        if self.on("local"):
            self.lmctl.poll()

    def _detect(self):
        self.detect = S.detect_all(self.st.data)

    def _fast(self):
        self._make_local()
        if self.on("local"):
            self.local.tick()
            if self.local.slots_status != self._slots_logged:
                self._slots_logged = self.local.slots_status
                log_debug(f"local slots: {self.local.slots_status} (port {self.local.port})")
        enabled = {k: self.on(k) for k in S.SOURCES}
        seen = set(self.st.get("seen") or [])
        new = [k for k, v in self.detect.items() if v.get("status") == "ok" and not enabled.get(k) and k not in seen]
        st = {"ts": time.time(), "version": VERSION, "enabled": enabled, "detect": self.detect, "new": new,
              "claude": self.claude.build() if enabled["claude"] else None,
              "codex": self.codex.state if enabled["codex"] else None,
              "local": self.local.build(self.gpu) if enabled["local"] else None,
              "lmctl": self.lmctl.build() if enabled["local"] else None,
              "warn": self.st.get("warn_percent", 70), "danger": self.st.get("danger_percent", 90)}
        with self.lock:
            self.state = st

    def kick(self):
        """设置改动后立刻刷新一遍。"""
        self.claude.next_poll = 0
        self.codex.kick()
        if self.local:
            self.local._last_full = 0
        for fn in (self._detect, self._slow, self._claude, self._codex_api, self._gpu, self._fast):
            threading.Thread(target=lambda f=fn: self._safe(f), daemon=True).start()

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            log_error("kick")

    def start(self):
        for name, iv, fn in (("slow", 15, self._slow), ("claude", 15, self._claude), ("codex_api", 15, self._codex_api),
                             ("gpu", 2, self._gpu),
                             ("lmctl", 2, self._lm), ("detect", 60, self._detect), ("fast", 1, self._fast)):
            threading.Thread(target=self._loop, args=(name, iv, fn), daemon=True).start()

    def snapshot(self):
        with self.lock:
            return self.state


ENGINE = None
SETTINGS = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status.json"):
            body = json.dumps(ENGINE.snapshot(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def start_http(st):
    host = "0.0.0.0" if st.get("http_lan") else "127.0.0.1"
    srv = ThreadingHTTPServer((host, int(st.get("http_port", 8765))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------------------------------------------------------------- 窗口
UI_STATE = os.path.join(DATA, "ui.json")


def load_ui():
    try:
        with open(UI_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ui(**kw):
    d = load_ui()
    d.update(kw)
    try:
        with open(UI_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def system_dark():
    if not IS_WIN:
        return False
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def uptime():
    """开机多少秒。"""
    if not IS_WIN:
        return 0.0
    import ctypes
    f = ctypes.windll.kernel32.GetTickCount64
    f.restype = ctypes.c_ulonglong
    return f() / 1000


def logon_age():
    """登录后过了多少秒（按桌面 explorer.exe 的启动时间算）。桌面还没起来返回 0，拿不到返回 None。"""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    shell = u32.GetShellWindow()
    if not shell:
        return 0.0
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(shell, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        ft = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(f) for f in ft]):
            return None
        created = ((ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime) / 1e7 - 11644473600
        return max(0.0, time.time() - created)
    finally:
        k32.CloseHandle(h)


def wait_for_desktop():
    """开机自启时才登录几秒，显卡驱动、显示器、其他开机程序都还在初始化，这时建出来的透明窗口
    有时会变成不透明，要退出重开才好。所以登录后不满 boot_delay 秒就先等一等再建窗口
    （采集线程照常在跑，只是窗口晚一点出现）。自动重启出来的进程不等。"""
    if not IS_WIN:
        return
    restarts = os.environ.get("AIW_RESTARTS", "0")
    age = logon_age()
    log_debug(f"start v{VERSION}: logon {'?' if age is None else f'{age:.0f}s'} ago, uptime {uptime():.0f}s, "
              f"restarts {restarts}")
    delay = float(SETTINGS.get("boot_delay", 40) or 0)
    if restarts != "0" or age is None or age >= delay:
        return
    t0 = time.time()
    while time.time() - t0 < delay + 20:  # 桌面一直没起来也最多等这么久
        age = logon_age()
        if age is not None and age >= delay:
            break
        time.sleep(1)
    log_debug(f"boot: waited {time.time() - t0:.0f}s for the desktop to settle")


def _dwm_set(hwnd, attr, value):
    import ctypes
    v = ctypes.c_int(value)
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)


def apply_transparency(hwnd, form=None):
    """关掉系统背景、边框、阴影和圆角，窗口按像素透明，外形和透明度完全由网页决定。"""
    import ctypes

    class MARGINS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
    if form is not None:
        try:  # 窗体底色必须是纯黑，DWM 才会把它当作透明
            from System import Action
            from System.Drawing import Color
            form.Invoke(Action(lambda: setattr(form, "BackColor", Color.Black)))
        except Exception:
            log_error("backcolor")
    m = MARGINS(-1, -1, -1, -1)
    ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
    _dwm_set(hwnd, 20, 1 if system_dark() else 0)
    _dwm_set(hwnd, 38, 1)            # DWMSBT_NONE（Win11）
    _dwm_set(hwnd, 33, 1)            # 不要系统圆角（Win11）
    _dwm_set(hwnd, 34, 0xFFFFFFFE)   # 不要边框（Win11）
    _dwm_set(hwnd, 2, 1)             # 关掉非客户区渲染（阴影）


def single_instance():
    """同一时间只运行一个；自动重启时会等旧进程退出。"""
    if not IS_WIN:
        return True
    import ctypes
    for _ in range(40):
        h = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\AIUsageWidget")
        if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            globals()["_MUTEX"] = h
            return True
        ctypes.windll.kernel32.CloseHandle(h)
        time.sleep(0.25)
    return False


class Api:
    """暴露给网页的接口（下划线开头的属性不会暴露）。"""

    def __init__(self):
        self._window = None
        self._hwnd = None
        self._show_again = None
        self._pf_handler = None
        self._size = (0, 0)

    # ---- 数据
    def state(self):
        return ENGINE.snapshot()

    def init(self):
        return {"style": SETTINGS.get("style"), "mode": SETTINGS.get("mode"), "lang": SETTINGS.get("lang"),
                "up": self._anchor()[1], "first_run": SETTINGS.first_run, "version": VERSION}

    def get_settings(self):
        return {"settings": SETTINGS.public(), "detect": ENGINE.detect, "autostart": S.autostart_enabled(),
                "data_dir": DATA, "version": VERSION}

    def save_settings(self, patch):
        SETTINGS.update(patch or {})
        if "sources" in (patch or {}):
            seen = set(SETTINGS.get("seen") or [])
            seen |= {k for k, v in ENGINE.detect.items() if v.get("status") == "ok"}
            SETTINGS.update({"seen": sorted(seen)})
        ENGINE.kick()
        return self.get_settings()

    def finish_setup(self):
        SETTINGS.first_run = False
        seen = {k for k, v in ENGINE.detect.items() if v.get("status") == "ok"}
        SETTINGS.update({"seen": sorted(seen | set(SETTINGS.get("seen") or []))})

    def redetect(self):
        ENGINE._detect()
        return self.get_settings()

    def set_autostart(self, on):
        try:
            return S.set_autostart(bool(on))
        except Exception:
            log_error("autostart")
            return S.autostart_enabled()

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)

    def open_data_dir(self):
        if IS_WIN:
            os.startfile(DATA)  # noqa

    def set_mode(self, mode):
        SETTINGS.update({"mode": mode})

    def set_style(self, style):
        SETTINGS.update({"style": style})

    # ---- 窗口
    def _rects(self):
        """窗口矩形和所在屏幕的可用区域（物理像素）。"""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]
        r = RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(r))
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(self._hwnd, 2), ctypes.byref(mi))
        sc = (user32.GetDpiForWindow(self._hwnd) or 96) / 96
        return r, mi.rcWork, sc

    def _anchor(self):
        """窗口在屏幕右半边就固定右边缘，在下半边就向上展开。"""
        try:
            if IS_WIN and self._hwnd:
                r, W, _ = self._rects()
                return (r.l + r.r) / 2 > (W.l + W.r) / 2, (r.t + r.b) / 2 > (W.t + W.b) / 2
            import webview
            sc = webview.screens[0]
            w = self._window
            return (w.x + w.width / 2) > sc.width / 2, (w.y + w.height / 2) > sc.height / 2
        except Exception:
            return True, False

    def fit(self, width, height):
        """按内容调整窗口大小。自己调 SetWindowPos 并带上 NOACTIVATE / NOZORDER：
        pywebview 自带的 resize 会激活窗口、把它提到最前，正在别的程序里打字时
        会抢走焦点，中文输入法的候选栏就被“吞掉”了。"""
        width, height = int(width), int(height)
        if (width, height) == self._size:
            return
        self._size = (width, height)
        try:
            if IS_WIN and self._hwnd:
                import ctypes
                r, W, sc = self._rects()
                right, up = (r.l + r.r) / 2 > (W.l + W.r) / 2, (r.t + r.b) / 2 > (W.t + W.b) / 2
                pw, ph = int(round(width * sc)), int(round(height * sc))
                x = r.r - pw if right else r.l
                y = r.b - ph if up else r.t
                SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0004, 0x0010, 0x0200
                ctypes.windll.user32.SetWindowPos(self._hwnd, 0, x, y, pw, ph,
                                                  SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
                return
            from webview.window import FixPoint
            right, up = self._anchor()
            fp = (FixPoint.EAST if right else FixPoint.WEST) | (FixPoint.SOUTH if up else FixPoint.NORTH)
            self._window.resize(width, height, fix_point=fp)
        except Exception:
            log_error("fit")

    def snap(self):
        """拖动松手后吸附到屏幕边缘 / 四角 / 上下边中间（按任务栏以外的可用区域算）。"""
        if not (IS_WIN and self._hwnd):
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = self._hwnd

            class RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))
            W = mi.rcWork
            sc = (user32.GetDpiForWindow(hwnd) or 96) / 96
            thr = SETTINGS.get("snap_distance", 48) * sc
            m = SETTINGS.get("snap_margin", 8) * sc
            w, h = r.r - r.l, r.b - r.t
            x0, y0 = r.l, r.t
            x, y = x0, y0
            if thr > 0:
                if abs(x - W.l) < thr:
                    x = W.l + m
                elif abs(x + w - W.r) < thr:
                    x = W.r - m - w
                elif abs(x + w / 2 - (W.l + W.r) / 2) < thr:
                    x = (W.l + W.r) / 2 - w / 2
                if abs(y - W.t) < thr:
                    y = W.t + m
                elif abs(y + h - W.b) < thr:
                    y = W.b - m - h
            x = max(W.l, min(x, W.r - w))  # 不允许拖出屏幕
            y = max(W.t, min(y, W.b - h))
            if (round(x), round(y)) == (x0, y0):
                return
            steps = 7
            for i in range(1, steps + 1):  # 缓动动画
                k = 1 - (1 - i / steps) ** 3
                user32.SetWindowPos(hwnd, 0, int(round(x0 + (x - x0) * k)), int(round(y0 + (y - y0) * k)),
                                    0, 0, 0x0001 | 0x0004 | 0x0010)
                time.sleep(0.014)
        except Exception:
            log_error("snap")

    def refresh(self):
        ENGINE.kick()

    # ---- 本地模型加载 / 卸载
    def local_load(self):
        ENGINE.lmctl.load()
        return ENGINE.lmctl.build()

    def local_unload(self):
        ENGINE.lmctl.unload()
        return ENGINE.lmctl.build()

    # ---- Codex 手动切号（只给窗口用，不走 HTTP 接口）
    def switch_state(self):
        if not SETTINGS.get("codex_switch"):
            return {"enabled": False}
        try:
            return ENGINE.switch.status()
        except Exception:
            log_error("codex switch state")
            return {"enabled": False}

    def _switch_call(self, fn, *a):
        if not SETTINGS.get("codex_switch"):
            return {"ok": False, "error": "failed"}
        try:
            fn(*a)
        except P_switch.SwitchError as e:
            return {"ok": False, "error": e.code}
        except Exception:
            log_error("codex switch")
            return {"ok": False, "error": "failed"}
        ENGINE.kick()
        return {"ok": True}

    def codex_switch(self, slot, close=False):
        return self._switch_call(ENGINE.switch.switch, str(slot), bool(close))

    def codex_save(self):
        return self._switch_call(ENGINE.switch.save_current)

    def codex_add(self, close=False):
        return self._switch_call(ENGINE.switch.begin_login, bool(close))

    def quit(self):
        ENGINE.stop.set()
        self._window.destroy()


def debug_variant():
    """排查用：data/debug_variant.txt 里写 0-4，逐步打开各项窗口特性（不存在则为正常模式）。"""
    try:
        with open(os.path.join(DATA, "debug_variant.txt"), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def win32_tweaks(api):
    """透明 + 不出现在任务栏/Alt+Tab + 全屏程序（游戏、视频）时自动隐藏。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    form = None
    try:
        form = api._window.native
        hwnd = int(form.Handle.ToInt64())
    except Exception:
        hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    api._hwnd = hwnd
    dv = debug_variant()
    if dv in (0, 1, 2):
        return
    try:
        apply_transparency(hwnd, form)
    except Exception:
        log_error("transparency")
    if dv == 3:
        return
    try:
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    except Exception:
        pass

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    def reapply(why):
        """DWM 的“玻璃”设置在显卡驱动重置、显示器重新连接后可能失效（窗口底色变成不透明的黑），重新套一次。"""
        try:
            apply_transparency(hwnd)
            if why:
                log_debug(f"transparency re-applied: {why}")
        except Exception:
            log_error("reapply transparency")

    shell = user32.GetShellWindow()
    desktop = user32.GetDesktopWindow()
    my_pid = os.getpid()
    GA_ROOT = 2
    WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80000, 0x08000000

    def win_info(w):
        cls, title = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(w, cls, 128)
        user32.GetWindowTextW(w, title, 128)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(w, ctypes.byref(pid))
        exe = ""
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            if h:
                buf, n = ctypes.create_unicode_buffer(260), wintypes.DWORD(260)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    exe = os.path.basename(buf.value)
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
        return cls.value, title.value, pid.value, exe

    def is_fullscreen(fg):
        """前台窗口铺满整块屏幕才算全屏；排除桌面、任务栏、自己，以及各种透明覆盖层（显卡/声卡浮窗等）。"""
        if not fg:
            return False, None
        fg = user32.GetAncestor(fg, GA_ROOT) or fg
        if fg in (hwnd, shell, desktop) or not user32.IsWindowVisible(fg):
            return False, None
        cls, title, pid, exe = win_info(fg)
        if pid == my_pid or cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False, None
        ex = user32.GetWindowLongW(fg, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW or (ex & WS_EX_LAYERED and ex & WS_EX_TRANSPARENT) or ex & WS_EX_NOACTIVATE:
            return False, None
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(fg, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if cloaked.value:
            return False, None
        r = RECT()
        user32.GetWindowRect(fg, ctypes.byref(r))
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(fg, 2), ctypes.byref(mi))
        m = mi.rcMonitor
        full = r.l <= m.l and r.t <= m.t and r.r >= m.r and r.b >= m.b
        return full, (cls, title[:40], exe)

    def show_again():
        """用 ShowWindow 隐藏再显示后，WebView2 不会自己恢复绘制（窗口在、内容空白），
        所以显示后在界面线程里把 WebView2 控件的可见性切一下，强制它重新渲染。"""
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        try:
            from System import Action
            wv = getattr(getattr(form, "browser", None), "webview", None)
            if form is not None and wv is not None:
                def kick():
                    wv.Visible = False
                    wv.Visible = True
                    form.Refresh()
                form.Invoke(Action(kick))
        except Exception:
            log_error("show_again")
        reapply("shown again")
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # 重新置顶，不抢焦点

    api._show_again = show_again

    def display_sig():
        sm = user32.GetSystemMetrics  # 显示器数量 + 虚拟桌面范围 + 本窗口 DPI
        return sm(80), sm(76), sm(77), sm(78), sm(79), user32.GetDpiForWindow(hwnd)

    def watch():
        hidden, streak = False, 0
        sig, t_start, t_reapply = display_sig(), time.time(), time.time()
        while not ENGINE.stop.is_set():
            try:
                s2 = display_sig()
                if s2 != sig:
                    reapply(f"display changed {sig} -> {s2}")
                    sig, t_reapply = s2, time.time()
                elif time.time() - t_start < 300 and time.time() - t_reapply > 30:
                    reapply(None)  # 开机后显卡驱动 / 显示器可能还会重置一次，前 5 分钟定时补一次
                    t_reapply = time.time()
                fs, info = (False, None)
                if SETTINGS.get("hide_on_fullscreen", True):
                    fs, info = is_fullscreen(user32.GetForegroundWindow())
                streak = streak + 1 if fs else 0
                if streak >= 2 and not hidden:  # 连续两次（约 3 秒）都是全屏才隐藏，避免误触
                    log_debug(f"hide for fullscreen window {info}")
                    user32.ShowWindow(hwnd, 0)
                    hidden = True
                elif not fs and hidden:
                    log_debug("show (fullscreen window gone)")
                    show_again()
                    hidden = False
            except Exception:
                log_error("fullscreen watch")
            time.sleep(1.5)
    threading.Thread(target=watch, daemon=True).start()


def restart_self(api, why, env_count=None):
    """重启自己。env_count 是额外要 +1 的计数（环境变量名），用来限制同一种原因的重启次数。"""
    log_debug(f"restart: {why}")
    try:
        import subprocess
        cmd = [sys.executable] if paths.FROZEN else [sys.executable, os.path.abspath(__file__)]
        env = dict(os.environ)
        for k in ("AIW_RESTARTS", env_count):
            if k:
                env[k] = str(int(env.get(k, "0") or 0) + 1)
        subprocess.Popen(cmd, cwd=paths.ROOT, env=env, creationflags=0x08000000 if IS_WIN else 0)
    except Exception:
        log_error("restart")
        return
    ENGINE.stop.set()
    try:
        api._window.destroy()
    except Exception:
        pass
    finally:
        os._exit(0)


def hook_process_failed(api, last=True):
    """订阅 WebView2 的 ProcessFailed：GPU 进程崩溃时 JS 还活着、窗口却画不出东西（透明窗口=直接消失），
    普通的心跳检测发现不了，所以直接听这个事件，任何子进程挂掉都自动重启。"""
    try:
        from System import Action
        form = api._window.native
        wv = form.browser.webview

        def attach():
            core = wv.CoreWebView2
            if core is None:
                raise RuntimeError("CoreWebView2 not ready")

            def on_failed(sender, args):
                try:
                    kind = str(args.ProcessFailedKind)
                except Exception:
                    kind = "?"
                log_debug(f"webview process failed: {kind}")
                threading.Thread(target=restart_self, args=(api, f"webview {kind}"), daemon=True).start()
            core.ProcessFailed += on_failed
            api._pf_handler = on_failed
        form.Invoke(Action(attach))
        log_debug("webview crash hook attached")
        return True
    except Exception:
        if last:  # 启动时 WebView2 常常还没就绪，会反复重试，只有最后一次失败才记
            log_error("hook_process_failed")
        return False


SOFTWARE_GL = ("swiftshader", "basic render", "warp", "llvmpipe", "software")


def run_js(api, code, timeout=8):
    """在网页里执行一段 JS；超时或出错返回 (False, 异常)。"""
    res = [None]

    def probe():
        try:
            res[0] = api._window.evaluate_js(code)
        except Exception as e:  # noqa
            res[0] = e
    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or isinstance(res[0], Exception):
        return False, res[0]
    return True, res[0]


def check_renderer(api, last):
    """WebView2 的 GPU 进程出问题（开机时显卡还没就绪、显存被占满等）后，Chromium 会整个进程
    改用软件渲染，透明窗口就可能变成不透明，而且不会自己恢复。查一下 WebGL 用的是什么渲染器：
    这台电脑以前用过显卡、现在却是软件渲染 / 拿不到 WebGL，就重启自己（最多连续 2 次）。"""
    ok, r = run_js(api, "window.__gpu ? window.__gpu() : ''")
    if not ok or not r:
        return last
    r = str(r)[:160]
    if r != last:
        log_debug(f"webview renderer: {r}")
    soft = r == "none" or any(k in r.lower() for k in SOFTWARE_GL)
    if not soft:
        if load_ui().get("gpu_renderer") != r:
            save_ui(gpu_renderer=r)
        return r
    if load_ui().get("gpu_renderer") and not ENGINE.stop.is_set():  # 这台电脑本来能用显卡
        n = int(os.environ.get("AIW_GPU_RESTARTS", "0") or 0)
        if n < 2:
            restart_self(api, f"webview fell back to software rendering ({r})", env_count="AIW_GPU_RESTARTS")
        elif r != last:
            log_debug("still software rendering after 2 restarts, leaving it")
    return r


def health_check(api):
    """WebView2 偶尔会崩（窗口变空白/消失），检测到就自动重启自己。"""
    fails = 0
    t0 = time.time()
    if IS_WIN:  # 崩溃监听一就绪就挂上：开机时 GPU 进程常常在头几秒就出问题
        while not ENGINE.stop.is_set() and not hook_process_failed(api, last=time.time() - t0 > 60):
            if time.time() - t0 > 60:
                break
            time.sleep(1)
    time.sleep(max(0.0, 15 - (time.time() - t0)))  # 开机时页面加载慢，留点时间再开始探测
    gpu_check = IS_WIN and SETTINGS.get("webview_gpu", True)
    renderer, next_gpu = None, 0
    while not ENGINE.stop.is_set():
        ok, r = run_js(api, "1+1")
        ok = ok and r == 2
        fails = 0 if ok else fails + 1
        if fails >= 2 and not ENGINE.stop.is_set():
            restart_self(api, f"webview unresponsive ({r!r})"[:200])
        if ok and gpu_check and time.time() >= next_gpu:
            # 刚启动的 5 分钟里每 20 秒查一次，之后每 5 分钟
            next_gpu = time.time() + (20 if time.time() - t0 < 300 else 300)
            renderer = check_renderer(api, renderer)
        time.sleep(5)


def file_watcher(api):
    """开发模式：改了 Python 代码自动重启，改了界面自动刷新。"""
    import glob

    def mt(fs):
        out = []
        for f in fs:
            try:
                out.append(os.path.getmtime(f))
            except OSError:
                out.append(0)
        return out
    code_files = lambda: sorted(glob.glob(os.path.join(paths.APP_DIR, "*.py")) +  # noqa: E731
                                glob.glob(os.path.join(paths.APP_DIR, "providers", "*.py")))
    code, ui = mt(code_files()), mt([paths.UI_HTML])
    log_debug("watcher started")
    flag = os.path.join(DATA, "dev_hide_test")
    while not ENGINE.stop.is_set():
        time.sleep(1.0)
        if os.path.exists(flag) and api._hwnd:  # 开发自测：模拟“全屏隐藏后再显示”
            try:
                with open(flag, "r", encoding="utf-8") as f:
                    how = f.read().strip()
                os.remove(flag)
                import ctypes
                log_debug(f"dev hide test: {how}")
                if how == "quit":
                    api.quit()
                    continue
                ctypes.windll.user32.ShowWindow(api._hwnd, 0)
                time.sleep(2)
                if how == "old":
                    ctypes.windll.user32.ShowWindow(api._hwnd, 4)
                else:
                    api._show_again()
            except Exception:
                log_error("dev hide test")
        c2, u2 = mt(code_files()), mt([paths.UI_HTML])
        if c2 != code:
            time.sleep(0.8)
            code = c2
            restart_self(api, "code changed")
        if u2 != ui:
            ui = u2
            log_debug("ui changed, reloading")
            api._size = (0, 0)
            try:
                with open(paths.UI_HTML, "r", encoding="utf-8") as f:
                    api._window.load_html(f.read())  # load_url 会命中缓存，直接塞新内容
            except Exception:
                log_error("reload")


def main():
    global ENGINE, SETTINGS
    if not single_instance():
        return
    SETTINGS = S.Settings(DATA)
    ENGINE = Engine(SETTINGS)
    ENGINE.start()
    if SETTINGS.get("http_enabled", True):
        for _ in range(20):  # 自动重启时旧进程可能还占着端口
            try:
                start_http(SETTINGS)
                break
            except OSError:
                time.sleep(0.3)

    if not SETTINGS.get("webview_gpu", True):
        # 软件渲染：不占显存，但会让 Win11 微软拼音候选栏消失，只建议在 GPU 进程频繁崩溃时手动关掉
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "") + " --disable-gpu").strip()
    wait_for_desktop()
    import webview
    ui = load_ui()
    api = Api()
    W0, H0 = 240, 34
    x, y = ui.get("x"), ui.get("y")
    try:
        screens = [(s.x, s.y, s.x + s.width, s.y + s.height) for s in webview.screens]
    except Exception:
        screens = []
    primary = screens[0] if screens else (0, 0, 1920, 1080)
    if x is None:
        x, y = primary[2] - W0 - 24, primary[1] + 24
    else:
        w, h = ui.get("w", W0), ui.get("h", H0)
        cx, cy = x + w / 2, y + h / 2
        # 按窗口中心所在的那块屏幕还原（多显示器时不再被拉回主屏）
        L, T, R, B = next((s for s in screens if s[0] <= cx < s[2] and s[1] <= cy < s[3]), primary)
        if cx > (L + R) / 2:
            x = x + w - W0
        if cy > (T + B) / 2:
            y = y + h - H0
        x, y = max(L, min(x, R - W0)), max(T, min(y, B - H0))
    dv = debug_variant()
    if dv is not None:
        log_debug(f"debug variant {dv}")
    win = webview.create_window(
        TITLE, paths.UI_HTML, js_api=api, width=W0, height=H0, x=x, y=y, min_size=(60, 24),
        resizable=False, frameless=True, easy_drag=False,
        on_top=dv != 1, transparent=dv not in (0, 1, 2), hidden=dv == 0,
        focus=not (dv is None or dv >= 4),  # 正常模式 focus=False（WS_EX_NOACTIVATE，不抢焦点）
        background_color="#000000" if dv not in (0, 1, 2) else "#1E1E1D")
    api._window = win

    def on_moved(*args):
        try:  # 记住窗口四边，重启时按固定的那个角还原
            save_ui(x=win.x, y=win.y, w=win.width, h=win.height)
        except Exception:
            pass

    def on_shown():
        log_debug(f"shown v{VERSION} data={DATA}")
        if IS_WIN:
            try:
                win32_tweaks(api)
            except Exception:
                log_error("win32")
        if SETTINGS.get("dev_reload") and not paths.FROZEN:
            threading.Thread(target=file_watcher, args=(api,), daemon=True).start()
        threading.Thread(target=health_check, args=(api,), daemon=True).start()
    win.events.moved += on_moved
    win.events.shown += on_shown
    webview.start(debug=bool(os.environ.get("AIW_DEBUG")))
    ENGINE.stop.set()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_error("main")
        raise
