"""Codex 手动切号（默认关闭，在设置里打开）：在同一个 CODEX_HOME 里换 auth.json，会话、项目和设置都共用。

只做用户点按钮时的手动切换，没有按额度自动换号。安全上的约定：
  1. 保存的登录信息用 Windows DPAPI 加密（绑定当前 Windows 用户），只有正在用的 auth.json 是 Codex 自己的明文；
  2. 永远不调用 Codex 的退出登录（它会吊销令牌），也不刷新令牌；
  3. Codex 和切换工具都会换新 refresh token，同一份不能两边用，所以 Codex 还在运行、或 Cockpit Tools
     还开着时不动 auth.json；
  4. 当前是哪个身份只看 auth.json 里的账号和空间，不信“上次切到谁”的记录，防止把 A 的令牌存进 B 的位置；
  5. 令牌和解密后的内容不写日志、不返回给界面，也不进 /status.json。
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from .codex import fetch_workspace_name, identity, key_id
from .common import NO_WINDOW, now

IS_WIN = sys.platform == "win32"
CODEX_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"  # 微软商店版 Codex
PACKAGE_DIR = "\\windowsapps\\openai.codex_"
# 桌面版自己管理的程序不在安装目录里：app-server（codex.exe）就在第一个目录下，手里有令牌，切换前必须关掉
MANAGED_DIRS = ("\\appdata\\local\\openai\\codex\\", "\\.codex\\.sandbox-bin\\", "\\.codex\\plugins\\",
                "\\.cache\\codex-runtimes\\")
# 系统服务，不读登录信息，也关不掉（LocalSystem）
NOT_CODEX = {"codex-windows-sandbox-service.exe", "codex-windows-sandbox-setup.exe"}
COCKPIT_EXE = "cockpit-tools.exe"
ENTROPY = b"AIUsageWidget.codex_vault.v1"
MAX_BACKUPS = 10
MAX_LABEL = 24
NAME_EVERY = 24 * 3600  # 秒：空间名多久重新查一次（管理员可能改名）
NAME_RETRY = 120        # 秒：查失败（代理偶尔握手超时、断网）后多久再试
CLOSE_WAIT = 8   # 秒：发出关闭消息后等多久，还没退出（Codex 关窗后可能留在后台）就结束进程
KILL_WAIT = 5


class SwitchError(Exception):
    """code 会传给界面翻译成提示，不带任何令牌内容。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(name, data):
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    src = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    bin_ = _Blob(len(data), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    ben = _Blob(len(ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    UI_FORBIDDEN = 0x1
    ok = getattr(crypt32, name)(ctypes.byref(bin_), None, ctypes.byref(ben), None, None, UI_FORBIDDEN,
                                ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), name)
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data):
    return _dpapi("CryptProtectData", data)


def unprotect(blob):
    return _dpapi("CryptUnprotectData", blob)


# ---------------------------------------------------------------- 进程
class _ProcEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcEntry)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcEntry)]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    return k


def list_procs():
    """所有进程：[(pid, 小写文件名, 小写完整路径, 父进程 pid)]；没权限读路径的进程路径为空。"""
    if not IS_WIN:
        return []
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "snapshot")
    out = []
    try:
        e = _ProcEntry()
        e.dwSize = ctypes.sizeof(_ProcEntry)
        more = k.Process32FirstW(snap, ctypes.byref(e))
        while more:
            path = ""
            h = k.OpenProcess(0x1000, False, e.th32ProcessID)  # QUERY_LIMITED_INFORMATION
            if h:
                buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    path = buf.value
                k.CloseHandle(h)
            out.append((e.th32ProcessID, e.szExeFile.lower(), path.lower(), e.th32ParentProcessID))
            more = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def classify(procs):
    """package = 桌面版 Codex 的进程（确认后可以关闭）；foreign = 其他地方的 codex（终端里的 CLI 等，不替用户关）。"""
    res = {"package": [], "foreign": [], "cockpit": False}
    parent = {p[0]: p[3] for p in procs}
    in_package = {p[0] for p in procs if PACKAGE_DIR in p[2] and p[1] not in NOT_CODEX}

    def from_app(pid):
        """父进程一路往上有没有桌面版（PID 可能被重复使用，所以限制层数）。"""
        for _ in range(16):
            pid = parent.get(pid)
            if pid is None:
                return False
            if pid in in_package:
                return True
        return False
    for pid, name, path, _ in procs:
        if name == COCKPIT_EXE:
            res["cockpit"] = True
        elif name in NOT_CODEX:
            continue
        elif pid in in_package:
            res["package"].append(pid)
        elif name.startswith("codex") and name.endswith(".exe"):
            managed = any(d in path for d in MANAGED_DIRS)
            res["package" if managed or from_app(pid) else "foreign"].append(pid)
    return res


def close_package(pids):
    """先礼后兵：给窗口发关闭消息，等一会儿还在的（后台进程）再结束。只碰传进来的 pid。"""
    if not IS_WIN or not pids:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    wanted = set(pids)
    WM_CLOSE = 0x0010
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    def on_window(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in wanted and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
    user32.EnumWindows(EnumProc(on_window), 0)
    if _wait_gone(wanted, CLOSE_WAIT):
        return
    k = _kernel32()
    for pid in _alive(wanted):
        h = k.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if h:
            k.TerminateProcess(h, 1)
            k.CloseHandle(h)
    _wait_gone(wanted, KILL_WAIT)


def _alive(pids):
    pkg = set(classify(list_procs())["package"])
    return [p for p in pids if p in pkg]


def _wait_gone(pids, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        if not _alive(pids):
            return True
        time.sleep(0.25)
    return not _alive(pids)


def launch_codex():
    """explorer 的退出码不代表成败，所以不检查。"""
    if IS_WIN:
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + CODEX_AUMID], creationflags=NO_WINDOW)


# ---------------------------------------------------------------- 文件
def _write_atomic(path, data):
    """先写同目录的临时文件再替换：读的人只会看到完整的旧文件或新文件。"""
    tmp = path + ".aiw-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(15):  # 其他程序刚好在读时 Windows 不让替换，等一下
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 14:
                    raise
                time.sleep(0.2)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _parse(raw):
    try:
        return identity(json.loads(raw.decode("utf-8-sig")))
    except Exception:
        return None


# ---------------------------------------------------------------- 切换
class CodexSwitcher:
    def __init__(self, home, vault_dir, procs=list_procs, closer=close_package, launcher=launch_codex,
                 fetch_name=fetch_workspace_name):
        self.auth_path = os.path.join(home, "auth.json")
        self.dir = vault_dir
        self.backups = os.path.join(vault_dir, "backups")
        self.meta_path = os.path.join(vault_dir, "slots.json")
        self.procs, self.closer, self.launcher, self.fetch_name = procs, closer, launcher, fetch_name
        self.lock = threading.RLock()
        self._name_tried = {}

    # ---- 保存的身份（slots.json 只有显示用的信息，令牌都在加密文件里）
    def _meta(self):
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return {}
        d = d if isinstance(d, dict) else {}
        return {k: v for k, v in d.items() if os.path.exists(self._slot_path(k))}

    def _save_meta(self, meta):
        os.makedirs(self.dir, exist_ok=True)
        _write_atomic(self.meta_path, json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))

    def _slot_path(self, sid):
        return os.path.join(self.dir, sid + ".dpapi")

    def _read_slot(self, sid):
        """解出保存的 auth.json，并确认里面真的是这个身份。"""
        try:
            with open(self._slot_path(sid), "rb") as f:
                raw = unprotect(f.read())
        except Exception:
            raise SwitchError("slot_unreadable") from None
        ident = _parse(raw)
        if not ident or key_id(ident["key"]) != sid:
            raise SwitchError("slot_unreadable")
        return raw

    def _store(self, raw, ident, meta):
        sid = key_id(ident["key"])
        os.makedirs(self.dir, exist_ok=True)
        blob = protect(raw)
        if unprotect(blob) != raw:
            raise SwitchError("failed")
        _write_atomic(self._slot_path(sid), blob)
        old = meta.get(sid) or {}
        meta[sid] = {**old, "label": old.get("label") or self._default_label(ident, sid, meta),
                     "email": ident.get("email"), "plan": ident.get("plan"), "saved": time.time()}
        self._save_meta(meta)
        return sid

    @staticmethod
    def _default_label(ident, sid, meta):
        email = ident.get("email") or ""
        base = " ".join(x for x in (email.split("@")[0], ident.get("plan")) if x) or "Codex"
        taken = {m.get("label") for m in meta.values()}
        label = base if base not in taken else base + " " + sid[:4]
        return label[:MAX_LABEL]

    def _backup(self, raw, sid):
        """改动 auth.json 之前留一份（同样加密）。把它复制成 <sid>.dpapi 就能恢复。"""
        os.makedirs(self.backups, exist_ok=True)
        name = "%s-%s.dpapi" % (time.strftime("%Y%m%d-%H%M%S"), sid or "unknown")
        _write_atomic(os.path.join(self.backups, name), protect(raw))
        files = sorted(f for f in os.listdir(self.backups) if f.endswith(".dpapi"))
        for f in files[:-MAX_BACKUPS]:
            os.remove(os.path.join(self.backups, f))

    # ---- auth.json
    def _read_auth(self):
        """-> (原始内容, 身份)。文件不存在是 (None, None)；存在但不是 ChatGPT 登录是 (内容, None)。"""
        try:
            with open(self.auth_path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return None, None
        return raw, _parse(raw)

    def _current_id(self, meta):
        """当前登录对应的身份；没有登录返回 None；不能动的情况直接报错。"""
        raw, ident = self._read_auth()
        if raw is None:
            return None, None, None
        if ident is None:
            raise SwitchError("auth_unknown")  # API key 登录或文件损坏：没法保存回去，不覆盖
        sid = key_id(ident["key"])
        if sid not in meta:
            raise SwitchError("unsaved")       # 覆盖了就丢了，先让用户保存
        return raw, ident, sid

    def _stop_codex(self, close):
        c = classify(self.procs())
        if c["cockpit"]:
            raise SwitchError("cockpit")
        if c["foreign"]:
            raise SwitchError("foreign")
        if c["package"]:
            if not close:
                raise SwitchError("running")
            for _ in range(2):  # 关闭途中可能又拉起新的子进程，还有就再关一次
                self.closer(c["package"])
                c = classify(self.procs())
                if not c["package"]:
                    return
            raise SwitchError("close_failed")

    # ---- 操作
    def status(self):
        with self.lock:
            meta = self._meta()
            raw, ident = self._read_auth()
            cur = key_id(ident["key"]) if ident else None
            c = classify(self.procs())
            slots = [{"id": k, "label": v.get("label"), "email": v.get("email"), "plan": v.get("plan"),
                      "current": k == cur} for k, v in meta.items()]
            for s in slots:  # 不同账号的空间可能同名：加上邮箱前缀区分
                if sum(x["label"] == s["label"] for x in slots) > 1 and s["email"]:
                    s["show"] = s["label"] + " · " + s["email"].split("@")[0]
            for s in slots:
                s["label"] = s.pop("show", s["label"])
            slots.sort(key=lambda s: (s["label"] or "").lower())
            return {"enabled": True, "slots": slots, "running": bool(c["package"]), "foreign": bool(c["foreign"]),
                    "cockpit": c["cockpit"], "no_login": raw is None, "auth_unknown": raw is not None and not ident,
                    "unsaved": bool(ident) and cur not in meta}

    def save_current(self):
        """把当前登录加密保存成一个身份（新增或更新）。不动 auth.json，Codex 开着也可以。"""
        with self.lock:
            raw, ident = self._read_auth()
            if raw is None:
                raise SwitchError("no_login")
            if ident is None:
                raise SwitchError("auth_unknown")
            return self._store(raw, ident, self._meta())

    def switch(self, sid, close=False):
        with self.lock:
            meta = self._meta()
            if sid not in meta:
                raise SwitchError("slot_unreadable")
            target = self._read_slot(sid)     # 目标读不出来就什么都不动
            _, _, cur = self._current_id(meta)
            if cur == sid:
                return
            self._stop_codex(close)
            # Codex 退出前可能刚换过令牌，或者刚换了空间，所以关掉之后重新读一遍
            raw, ident, cur = self._current_id(meta)
            if raw is not None:
                self._backup(raw, cur)
                self._store(raw, ident, meta)
            if cur != sid:
                _write_atomic(self.auth_path, target)
            self.launcher()

    def begin_login(self, close=False):
        """添加身份：当前登录存好后移走 auth.json（不走退出登录，令牌不会被吊销），Codex 就会显示登录页。"""
        with self.lock:
            meta = self._meta()
            self._current_id(meta)
            self._stop_codex(close)
            raw, ident, cur = self._current_id(meta)
            if raw is not None:
                self._backup(raw, cur)
                self._store(raw, ident, meta)
                os.remove(self.auth_path)
            self.launcher()

    def refresh_names(self):
        """用空间名给身份命名（查官方账号接口，只读）。个人空间没有名字，或查不到时保留原来的名字。"""
        t = now()
        with self.lock:
            meta = self._meta()
            due = {k for k, v in meta.items() if t - (v.get("named") or 0) >= NAME_EVERY
                   and t - self._name_tried.get(k, 0) >= NAME_RETRY}
            idents = [i for i in self.identities() if key_id(i["key"]) in due] if due else []
        for ident in idents:  # 网络请求不占着锁
            sid = key_id(ident["key"])
            self._name_tried[sid] = t
            if ident.get("exp") and ident["exp"] < t + 30:
                continue
            try:
                name = self.fetch_name(ident)
            except Exception:
                continue
            with self.lock:
                meta = self._meta()
                if sid in meta:
                    if name:
                        meta[sid]["label"] = name[:MAX_LABEL]
                    meta[sid]["named"] = t
                    self._save_meta(meta)

    def identities(self):
        """保存的身份（含 access token，只在内存里用来查额度；不刷新）。"""
        with self.lock:
            out = []
            for sid in self._meta():
                try:
                    ident = _parse(self._read_slot(sid))
                except SwitchError:
                    continue
                if ident:
                    out.append(ident)
            return out
