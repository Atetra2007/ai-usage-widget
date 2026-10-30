"""Codex：会话日志（token 用量 + 额度快照）+ 登录信息（区分账号）+ 官方额度接口（实时额度）。

同一台电脑上切换多个 Codex 账号时，日志里的额度快照不带账号信息，只取最新一条就会在几个号之间来回跳。
所以按账号分开记录：
  1. 读 auth.json 知道当前登录的是哪个号（只读，从令牌里解出邮箱和账号 ID）；
  2. 用额度窗口的重置时间当指纹：一个号同一时间只有一个 5 小时窗口和一个周窗口，
     指纹对得上的快照归到那个号；跟某个号已知窗口冲突的快照，肯定不是那个号的；
  3. 用 auth.json 和切换工具保存的其他账号的 access token 查官方额度接口（Codex /status 用的同一个），
     拿到每个号准确的实时额度。只读：不刷新、不改写任何令牌，令牌也不写进小组件的数据文件。
"""
import base64
import glob
import hashlib
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from .common import History, local_date, now, parse_iso, today, window_info  # noqa: F401

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"
SAME = 150             # 秒：同一个窗口在不同请求里报的重置时间会差一两秒
MAX_SHOWN = 4          # 界面上最多显示几个账号
DISCOVER_EVERY = 600   # 多久重新找一次切换工具保存的账号

# 切换工具常把其他账号的登录信息放在这些地方（只读；找不到也没关系，只是其他号没法实时查询）
TOOL_HOME_DIRS = (".codex-switch", ".codex_switch", ".codex-switcher", ".codex-accounts", ".codex_accounts",
                  ".codex-auth", ".codex-profiles", ".cc-switch", ".antigravity_cockpit", ".cockpit-tools",
                  ".cli-proxy-api", "cliproxyapi")
TOOL_APPDATA_DIRS = ("com.antigravity.cockpit-tools", "cockpit-tools", "codex-switch", "codex-switcher",
                     "CodexSwitcher", "com.ccswitch.desktop", "cc-switch")
CODEX_SUBDIRS = ("accounts", "profiles", "auths", "auth", "switch", "backup", "backups")
SKIP_DIRS = {"sessions", "archived_sessions", "log", "logs", "cache", "Cache", "Code Cache", "GPUCache",
             "node_modules", ".git", "tmp", "EBWebView", "shell_snapshots", "plugins"}


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def default_dir():
    return os.path.join(codex_home(), "sessions")


def detect(sessions_dir=None):
    d = sessions_dir or default_dir()
    if glob.glob(os.path.join(d, "**", "rollout-*.jsonl"), recursive=True):
        return {"status": "ok", "path": d}
    if os.path.isdir(os.path.dirname(d)):
        return {"status": "no_sessions", "path": d}
    return {"status": "missing", "path": d}


# ---------------------------------------------------------------- 账号身份
def jwt_payload(tok):
    """只解出 JWT 的内容（不校验签名，只用来认出是哪个号）。"""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        v = json.loads(base64.urlsafe_b64decode(p.encode()).decode("utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def identity(d):
    """auth.json 格式（或切换工具保存的类似结构）-> 账号身份 + access token；不是 ChatGPT 登录信息就返回 None。"""
    if not isinstance(d, dict):
        return None
    t = d.get("tokens") if isinstance(d.get("tokens"), dict) else d
    at = t.get("access_token") or t.get("accessToken")
    if not isinstance(at, str) or at.count(".") != 2:
        return None
    it = t.get("id_token") or t.get("idToken")
    if isinstance(it, dict):
        it = it.get("raw_jwt") or it.get("rawJwt")
    ap = jwt_payload(at)
    ip = jwt_payload(it) if isinstance(it, str) else {}
    auth = ip.get(AUTH_CLAIM) or ap.get(AUTH_CLAIM)
    if not isinstance(auth, dict):
        return None  # 不是 ChatGPT 账号的令牌
    acct = t.get("account_id") or t.get("accountId") or auth.get("chatgpt_account_id")
    email = ip.get("email") or (ap.get(PROFILE_CLAIM) or {}).get("email") or d.get("email")
    email = email if isinstance(email, str) and email else None
    uid = auth.get("chatgpt_user_id") or auth.get("user_id") or ip.get("sub") or ap.get("sub")
    if not acct and not email:
        return None
    return {"key": f"{acct or '-'}/{(email or uid or '-').lower()}", "email": email, "account_id": acct,
            "plan": auth.get("chatgpt_plan_type"), "token": at, "exp": ap.get("exp")}


def _walk(obj, found, depth=0):
    if depth > 6 or len(found) > 40:
        return
    if isinstance(obj, dict):
        idt = identity(obj)
        if idt:
            found.append(idt)
            return
        for v in obj.values():
            _walk(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            _walk(v, found, depth + 1)
    elif isinstance(obj, str) and obj[:1] in "{[" and ("access_token" in obj or "accessToken" in obj):
        try:
            _walk(json.loads(obj), found, depth + 1)
        except Exception:
            pass


def read_identities(path):
    """从一个 JSON 文件或 SQLite 数据库里找出所有 ChatGPT 登录信息（只读）。"""
    found = []
    try:
        size = os.path.getsize(path)
        if path.lower().endswith(".json"):
            if size > 4 * 1024 * 1024:
                return found
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                _walk(json.load(f), found)
        elif size < 64 * 1024 * 1024:
            con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
                for tb in tables[:60]:
                    try:
                        for row in con.execute('select * from "%s" limit 500' % tb.replace('"', '""')):
                            for v in row:
                                if isinstance(v, bytes):
                                    try:
                                        v = v.decode("utf-8")
                                    except Exception:
                                        continue
                                if isinstance(v, str) and ("access_token" in v or "accessToken" in v):
                                    _walk(v, found)
                    except sqlite3.Error:
                        continue
            finally:
                con.close()
    except Exception:
        pass
    return found


def candidate_files(extra=(), home=None):
    """切换工具可能保存账号的文件。extra = 用户在 settings.json 里指定的文件或目录。"""
    home = home or codex_home()
    user = os.path.expanduser("~")
    roots = [(p, 3) for p in (extra or []) if isinstance(p, str) and p]
    roots.append((home, 0))  # 只看第一层，例如 auth-work.json、auth.json.bak
    roots += [(os.path.join(home, s), 3) for s in CODEX_SUBDIRS]
    roots += [(os.path.join(user, s), 3) for s in TOOL_HOME_DIRS]
    for env in ("APPDATA", "LOCALAPPDATA"):
        b = os.environ.get(env)
        if b:
            roots += [(os.path.join(b, s), 3) for s in TOOL_APPDATA_DIRS]
    out = []
    for root, depth in roots:
        root = os.path.expandvars(os.path.expanduser(root))
        if os.path.isfile(root):
            out.append(root)
            continue
        if not os.path.isdir(root):
            continue
        base = root.rstrip("\\/").count(os.sep)
        for d, dirs, files in os.walk(root):
            if d.count(os.sep) - base >= depth:
                dirs[:] = []
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                fl = f.lower()
                if fl.endswith(".json") or (root != home and fl.endswith((".db", ".sqlite", ".sqlite3"))):
                    out.append(os.path.join(d, f))
            if len(out) > 400:
                break
    seen, res = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


# ---------------------------------------------------------------- 额度窗口
def _g(w, *names):
    for n in names:
        if w.get(n) is not None:
            return w[n]
    return None


def norm_windows(rl, ts=None):
    """额度快照 -> {"five_hour": {used, resets_at, mins}, "weekly": {...}}；日志和接口两种格式都认。"""
    out = {}
    if not isinstance(rl, dict):
        return out
    for key, alts in (("primary", ("primary_window", "primaryWindow")),
                      ("secondary", ("secondary_window", "secondaryWindow"))):
        w = _g(rl, key, *alts)
        if not isinstance(w, dict):
            continue
        used = _g(w, "used_percent", "usedPercent")
        if used is None:
            continue
        mins = _g(w, "window_minutes", "window_duration_mins", "windowDurationMins")
        if not mins:
            secs = _g(w, "limit_window_seconds", "limitWindowSeconds")
            mins = (int(secs) + 59) // 60 if secs else None
        # 按窗口长度归类：只有每周额度的套餐里，primary 就是 7 天窗口
        name = ("weekly" if mins >= 1440 else "five_hour") if mins else ("five_hour" if key == "primary" else "weekly")
        if name in out:
            continue
        resets = parse_iso(_g(w, "resets_at", "reset_at", "resetsAt", "resetAt"))
        if resets is None and ts:  # 旧版 Codex / 接口的相对时间格式
            secs = _g(w, "resets_in_seconds", "reset_after_seconds", "resetAfterSeconds")
            if secs is not None:
                resets = ts + float(secs)
        out[name] = {"used": max(0.0, min(100.0, float(used))), "resets_at": resets,
                     "mins": int(mins) if mins else (300 if name == "five_hour" else 10080)}
    return out


def parse_api(data):
    """官方额度接口的返回 -> (windows, plan)。"""
    if not isinstance(data, dict):
        return {}, None
    plan = data.get("plan_type") or data.get("planType")
    rl = data.get("rate_limit") or data.get("rateLimit")
    if not isinstance(rl, dict):
        by = data.get("rate_limits_by_limit_id") or data.get("rateLimitsByLimitId") or {}
        rl = (by.get("codex") if isinstance(by, dict) else None) or data.get("rate_limits") or data.get("rateLimits")
    return norm_windows(rl, now()), plan


def fetch_usage(ident, timeout=10):
    h = {"Authorization": "Bearer " + ident["token"], "User-Agent": "codex-cli", "Accept": "application/json"}
    if ident.get("account_id"):
        h["ChatGPT-Account-Id"] = ident["account_id"]
    req = urllib.request.Request(CODEX_USAGE_URL, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fp(w):
    """指纹 = 最长那个窗口的 (长度, 重置时间)。"""
    best = None
    for x in w.values():
        if x.get("resets_at") and (best is None or x["mins"] > best[0]):
            best = (x["mins"], x["resets_at"])
    return best


def _same(a, b):
    return a[0] == b[0] and abs(a[1] - b[1]) <= SAME


def _overlap(a, b):
    """同样长度、重置时间不同、时间上又重叠的两个窗口，不可能属于同一个号。"""
    return a[0] == b[0] and SAME < abs(a[1] - b[1]) < a[0] * 60 - SAME


# ---------------------------------------------------------------- 账号记录（data/codex_accounts.json，不含令牌）
class Accounts:
    def __init__(self, path):
        self.path = path
        self.d = {"accounts": {}, "switches": [], "checked": 0}
        self.dirty = False
        self._saved_checked = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("accounts"), dict):
                self.d.update(d)
        except Exception:
            pass

    @property
    def accs(self):
        return self.d["accounts"]

    def acc(self, key, ident=None):
        a = self.accs.get(key)
        if a is None:
            a = self.accs[key] = {"snap": None, "fps": [], "seen": 0}
            self.dirty = True
        if ident:
            for k in ("email", "plan"):
                if ident.get(k) and a.get(k) != ident[k]:
                    a[k] = ident[k]
                    self.dirty = True
        return a

    def observe(self, key, mtime):
        """记录 auth.json 里账号的变化：切换时间 = max(文件修改时间, 上次看到旧账号的时间)。"""
        t = now()
        sw = self.d["switches"]
        if not sw or sw[-1][1] != key:
            start = (mtime or t) if not sw else max(mtime or 0, self.d.get("checked") or 0)
            sw.append([min(start, t), key])
            del sw[:-100]
            self.dirty = True
        self.d["checked"] = t
        if t - self._saved_checked > 300:
            self.dirty = True

    def current_at(self, ts):
        for st, key in reversed(self.d["switches"]):
            if st <= ts:
                return key
        return None

    def owner_by_fp(self, w):
        fp = _fp(w)
        if not fp:
            return None
        hit = [k for k, a in self.accs.items() if any(_same(f, fp) for f in a["fps"])]
        hit.sort(key=lambda k: bool(self.accs[k].get("anon")))
        return hit[0] if hit else None

    def conflicts(self, key, w):
        fps = self.accs[key]["fps"]
        for x in w.values():
            if x.get("resets_at") and any(_overlap((x["mins"], x["resets_at"]), f) for f in fps):
                return True
        return False

    def new_anon(self, w):
        fp = _fp(w)
        key = "anon:%d" % int(fp[1])
        a = self.acc(key)
        a["anon"] = True
        return key

    def update(self, key, ts, w, src, plan=None):
        a = self.accs[key]
        s = a.get("snap")
        if s is None or ts > s["ts"] + 0.5:
            a["snap"] = {"ts": ts, "src": src, "w": w}
            self.dirty = True
        if plan and not a.get("plan"):
            a["plan"] = plan
        a["seen"] = max(a.get("seen") or 0, ts)
        new = [(x["mins"], x["resets_at"]) for x in w.values() if x.get("resets_at")]
        if src == "api":  # 接口是准的：跟它冲突的旧指纹是之前归错的，删掉
            keep = [f for f in a["fps"] if not any(_overlap(tuple(f), n) for n in new)]
            if len(keep) != len(a["fps"]):
                a["fps"] = keep
                self.dirty = True
        for n in new:
            if not any(_same(tuple(f), n) for f in a["fps"]):
                a["fps"].append([n[0], n[1]])
                self.dirty = True
        a["fps"] = sorted(a["fps"], key=lambda f: f[1])[-8:]
        self._merge_anon(key)

    def _merge_anon(self, key):
        a = self.accs.get(key)
        if not a:
            return
        for k2 in list(self.accs):
            b = self.accs[k2]
            if k2 == key or not b.get("anon"):
                continue
            if any(_same(tuple(f), tuple(g)) for f in a["fps"] for g in b["fps"]):
                if b.get("snap") and (not a.get("snap") or b["snap"]["ts"] > a["snap"]["ts"]):
                    a["snap"] = b["snap"]
                for g in b["fps"]:
                    if not any(_same(tuple(f), tuple(g)) for f in a["fps"]):
                        a["fps"].append(g)
                a["seen"] = max(a.get("seen") or 0, b.get("seen") or 0)
                del self.accs[k2]
                self.dirty = True

    def prune(self, keep):
        t = now()
        for k in list(self.accs):
            a = self.accs[k]
            if k in keep:
                continue
            fp = _fp((a.get("snap") or {}).get("w") or {})
            if a.get("anon") and (not fp or fp[1] < t):  # 其他号的窗口都过期了，没必要再显示
                del self.accs[k]
                self.dirty = True
            elif not a.get("anon") and (a.get("seen") or 0) < t - 45 * 86400:
                del self.accs[k]
                self.dirty = True

    def save(self):
        if not self.dirty or not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self.dirty = False
            self._saved_checked = self.d.get("checked") or 0
        except Exception:
            pass


def _tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 采集
class CodexCollector:
    def __init__(self, sessions_dir, cache, history, data_dir=None, opts=None):
        self.dir = sessions_dir
        self.home = os.path.dirname(sessions_dir)
        self.auth_path = os.path.join(self.home, "auth.json")
        self.cache = cache
        self.history = history
        self.opts = opts or (lambda: {})
        self.lock = threading.RLock()
        self.store = Accounts(os.path.join(data_dir, "codex_accounts.json") if data_dir else None)
        self.current = None      # auth.json 里当前登录的号（令牌只在内存里）
        self.found = {}          # 切换工具里找到的号：key -> identity
        self.found_paths = {}
        self.found_at = 0
        self._found_cfg = None
        self.next_poll = {}
        self.tok = {}
        self.err = {}
        self.backoff = {}
        self.latest = None       # 读不到登录信息时（例如令牌存在系统钥匙串里）退回到“最新一条快照”
        self.files = 0
        self.assign_stats = {}
        self.state = {"ok": False}

    # ---- 日志
    def _parse(self, path):
        daily = defaultdict(lambda: {"total": 0, "fresh": 0})
        prev = None
        rls = {}  # 指纹 -> 这个文件里该窗口最新的一条快照（一个会话里途中换号也能分开）
        start = None  # 会话开始时间：Codex 启动时读一次登录信息，切号前开的会话还用原来的号
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n == 0:
                    try:
                        start = parse_iso((json.loads(line) or {}).get("timestamp"))
                    except Exception:
                        start = None
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
                if r.get("limit_id") not in (None, "codex"):
                    continue  # 其他功能的单独额度，不是 Codex 主额度
                w = norm_windows(r, ts)
                fp = _fp(w)
                if w and ts:
                    k = "%d:%d" % (fp[0], round(fp[1] / 60)) if fp else "-"
                    if k not in rls or ts >= rls[k][0]:
                        rls[k] = [ts, w, r.get("plan_type"), start]
        snaps = sorted(rls.values(), key=lambda x: x[0])[-6:]
        return {"daily": dict(daily), "snaps": snaps}

    def _read_current(self):
        ident, mtime = None, None
        try:
            mtime = os.path.getmtime(self.auth_path)
            with open(self.auth_path, "r", encoding="utf-8") as f:
                ident = identity(json.load(f))
        except Exception:
            ident = None
        self.current = ident
        with self.lock:
            if ident:
                self.store.acc(ident["key"], ident)
            if ident or mtime:  # 退出登录 / 改用 API key 也记一笔，之后的快照就不算到原来的号上
                self.store.observe(ident["key"] if ident else None, mtime)

    def scan(self):
        files = glob.glob(os.path.join(self.dir, "**", "rollout-*.jsonl"), recursive=True)
        merged = defaultdict(lambda: {"total": 0, "fresh": 0})
        snaps = []
        for fp in files:
            try:
                st = os.stat(fp)
            except OSError:
                continue
            v = self.cache.get("codex2:" + fp, st)
            if v is None:
                try:
                    v = self._parse(fp)
                except Exception:
                    continue
                self.cache.put("codex2:" + fp, st, v)
            for day, vals in v["daily"].items():
                merged[day]["total"] += vals["total"]
                merged[day]["fresh"] += vals["fresh"]
            snaps += v.get("snaps") or []
        self.cache.prune("codex:", set())  # 旧版缓存格式
        self.cache.prune("codex2:", {"codex2:" + fp for fp in files})
        self.history.merge("codex", merged)
        self.files = len(files)
        self._read_current()
        self._discover()
        snaps.sort(key=lambda x: x[0])
        self.latest = snaps[-1] if snaps else None
        with self.lock:
            self._assign(snaps)
            keep = set(self.found) | ({self.current["key"]} if self.current else set())
            self.store.prune(keep)
            self.store.save()
        return self.build()

    def _assign(self, snaps):
        """把日志里的额度快照归到各个账号。拿不准的宁可跳过，也不归错。"""
        st = self.store
        stats = {"fp": 0, "login": 0, "anon": 0, "skip": 0}
        self.assign_stats = stats
        if not any(not a.get("anon") for a in st.accs.values()):
            return  # 一个号都认不出来（读不到 auth.json）：不分账号，按老办法显示最新快照
        t = now()
        for _ in range(2):  # 第一遍学到的指纹能帮第二遍认出更多快照
            for k in stats:
                stats[k] = 0
            for s in snaps:
                self._assign_one(s, t, stats)

    def _assign_one(self, s, t, stats):
        st = self.store
        ts, w, plan = s[0], s[1], s[2]
        start = s[3] if len(s) > 3 else None
        c1 = st.current_at(ts)                         # 快照那一刻登录的号
        c2 = st.current_at(start) if start else c1     # 会话开始时登录的号
        k = st.owner_by_fp(w)
        if k and st.accs[k].get("anon") and c1 == c2 and c1 is not None and not st.conflicts(c1, w):
            k = c1  # 之前记成“其他账号”的，现在知道是谁了（update 时会合并）
        if k:
            stats["fp"] += 1
        else:
            ok = [x for x, a in st.accs.items() if not a.get("anon") and not st.conflicts(x, w)]
            fp = _fp(w)
            if c1 == c2 and c1 is not None and c1 in ok:
                k = c1
            elif c1 != c2 and None not in (c1, c2) and (c1 in ok) != (c2 in ok):
                k = c1 if c1 in ok else c2  # 切号前后各一个号，另一个跟这条快照冲突
            elif not ok and fp and fp[1] > t:
                k = st.new_anon(w)  # 跟所有已知的号都冲突，窗口还没过期：先记成“其他账号”
            else:
                stats["skip"] += 1
                return
            stats["anon" if k.startswith("anon:") else "login"] += 1
        st.update(k, ts, w, "log", plan)

    # ---- 切换工具里保存的其他账号
    def _discover(self, force=False):
        o = self.opts() or {}
        cfg = (bool(o.get("others", True)), tuple(o.get("extra") or ()))
        if not cfg[0]:
            self.found, self.found_paths = {}, {}
            return
        if not force and cfg == self._found_cfg and now() - self.found_at < DISCOVER_EVERY:
            return
        found, paths = {}, {}
        me = os.path.normcase(os.path.abspath(self.auth_path))
        for path in candidate_files(cfg[1], self.home):
            if os.path.normcase(os.path.abspath(path)) == me:
                continue
            for ident in read_identities(path):
                k = ident["key"]
                if k not in found or (ident.get("exp") or 0) > (found[k].get("exp") or 0):
                    found[k] = ident
                    paths[k] = path
        self.found, self.found_paths, self.found_at, self._found_cfg = found, paths, now(), cfg
        with self.lock:
            for k, ident in found.items():
                self.store.acc(k, ident)

    def _targets(self):
        """要查询的账号：当前号 + 切换工具里的号；同一个号有多份令牌时用过期最晚的那份。"""
        out = dict(self.found)
        c = self.current
        if c:
            o = out.get(c["key"])
            if not o or (c.get("exp") or 0) >= (o.get("exp") or 0):
                out[c["key"]] = c
        return out

    # ---- 官方额度接口（后台线程每 15 秒调一次，按账号控制频率）
    def poll(self):
        o = self.opts() or {}
        if not o.get("live", True):
            return
        if self.current is None:
            self._read_current()
        self._discover()
        poll = max(60, int(o.get("poll") or 180))
        cur = self.current["key"] if self.current else None
        changed = False
        for key, ident in self._targets().items():
            h = _tok_hash(ident["token"])
            if self.tok.get(key) != h:  # 令牌换了（刷新 / 重新登录）：马上再查
                self.tok[key] = h
                self.next_poll[key] = 0
            if now() < self.next_poll.get(key, 0):
                continue
            every = poll if key == cur else max(600, poll)
            if ident.get("exp") and ident["exp"] < now() + 30:
                self.err[key] = "token_expired"
                self.next_poll[key] = now() + 600
                continue
            try:
                w, plan = parse_api(fetch_usage(ident))
                with self.lock:
                    self.store.acc(key, ident)
                    if plan and not self.store.accs[key].get("plan"):
                        self.store.accs[key]["plan"] = plan
                    if w:
                        self.store.update(key, now(), w, "api")
                self.err[key] = None
                self.backoff[key] = every
                self.next_poll[key] = now() + every
                changed = True
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.err[key] = "token_expired"
                    self.next_poll[key] = now() + 1800
                else:
                    self.err[key] = f"http_{e.code}"
                    b = self.backoff.get(key) or every
                    self.backoff[key] = min(1800, b * 2) if e.code in (403, 429) else every
                    self.next_poll[key] = now() + self.backoff[key]
            except Exception:
                self.err[key] = "network"
                self.next_poll[key] = now() + every
        if changed:
            with self.lock:
                self.store.save()
        self.build()

    def kick(self):
        self.next_poll.clear()
        self.found_at = 0

    # ---- 给界面的状态
    def _label(self, key, a, labels):
        email = a.get("email") or ""
        for k in (email, email.lower(), key):
            if k and labels.get(k):
                return str(labels[k])
        return email.split("@")[0][:16] if email else None

    def build(self):
        o = self.opts() or {}
        labels = o.get("labels") or {}
        cur = self.current["key"] if self.current else None
        with self.lock:
            accs = self.store.accs
            keys = [k for k, a in accs.items() if k == cur or a.get("snap")]
            keys.sort(key=lambda k: (k != cur, bool(accs[k].get("anon")), -(accs[k].get("seen") or 0)))
            keys = keys[:MAX_SHOWN]
            out, names = [], {}
            for k in keys:
                a = accs[k]
                snap = a.get("snap") or {}
                w = snap.get("w") or {}
                e = {"label": self._label(k, a, labels), "plan": a.get("plan"), "current": k == cur,
                     "anon": bool(a.get("anon")), "updated": snap.get("ts"), "src": snap.get("src"),
                     "error": self.err.get(k) if k in self.found or k == cur else None,
                     "live": bool(self.found.get(k) or k == cur)}
                for name in ("five_hour", "weekly"):
                    x = w.get(name)
                    e[name] = window_info(x["used"], x["resets_at"], x["mins"]) if x else None
                if e["label"]:
                    names.setdefault(e["label"], []).append(e)
                out.append(e)
            for same in names.values():  # 同一个邮箱的个人 / 团队空间：加上套餐区分
                if len(same) > 1:
                    for e in same:
                        e["label"] += " " + str(e["plan"] or "?")
            ca = accs.get(cur) if cur else None
        td, tt = self.history.totals("codex")
        st = {"ok": bool(self.files), "today": td, "total": tt, "five_hour": None, "weekly": None, "updated": None,
              "week": self.history.last_days("codex"), "accounts": out,
              "account": out[0]["label"] if out and out[0]["current"] else None,
              "error": self.err.get(cur) if cur else None}
        if ca is not None:
            snap = ca.get("snap") or {}
            st["updated"] = snap.get("ts")
            w = snap.get("w") or {}
        elif self.latest:  # 认不出账号：退回老办法
            st["updated"], w = self.latest[0], self.latest[1]
        else:
            w = {}
        for name in ("five_hour", "weekly"):
            x = w.get(name)
            if x:
                st[name] = window_info(x["used"], x["resets_at"], x["mins"])
        self.state = st
        return st


# ---------------------------------------------------------------- 本机自检
def _mask(email):
    if not email or "@" not in email:
        return email or "?"
    u, d = email.split("@", 1)
    return (u[:2] + "***" + u[-2:] if len(u) > 4 else u[:1] + "***") + "@" + d


def diagnose(out_path=None, live=True):
    """在本机跑一遍：列出找到的账号来源、当前账号、接口查询结果和日志快照归属。邮箱打码，不输出令牌。"""
    import tempfile
    from datetime import datetime
    from .common import FileCache
    lines = []

    def p(*a):
        lines.append(" ".join(str(x) for x in a))

    fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"  # noqa: E731
    tmp = tempfile.mkdtemp(prefix="aiw-codex-")
    cache = FileCache(os.path.join(tmp, "cache.json"))
    hist = History(os.path.join(tmp, "history.json"))
    c = CodexCollector(default_dir(), cache, hist, tmp, lambda: {"live": live})
    p("CODEX_HOME:", c.home)
    p("auth.json:", "存在" if os.path.exists(c.auth_path) else "不存在")
    c._read_current()
    cur = c.current
    p("当前账号:", _mask(cur["email"]) + " plan=" + str(cur["plan"]) + " 令牌到期=" + fmt(cur.get("exp"))
      if cur else "认不出（可能是 API key 登录或令牌存在系统钥匙串里）")
    c._discover(force=True)
    p("候选文件数:", len(candidate_files((), c.home)))
    if not c.found:
        p("切换工具里的账号: 没找到")
    for k, ident in c.found.items():
        p("切换工具里的账号:", _mask(ident["email"]), "plan=" + str(ident["plan"]), "令牌到期=" + fmt(ident.get("exp")),
          "来源=" + c.found_paths.get(k, "?"))
    if live:
        c.poll()
        for k, ident in c._targets().items():
            a = c.store.accs.get(k) or {}
            s = a.get("snap") or {}
            p("接口查询:", _mask(ident["email"]), "错误=" + str(c.err.get(k)), "额度=" + json.dumps(s.get("w"))[:300])
    c.scan()
    p("日志快照归属:", json.dumps(getattr(c, "assign_stats", {})), "日志文件数:", c.files)
    for k, a in c.store.accs.items():
        s = a.get("snap") or {}
        p("账号:", "其他账号(未识别)" if a.get("anon") else _mask(a.get("email")), "来源=" + str(s.get("src")),
          "时间=" + fmt(s.get("ts")), "额度=" + json.dumps(s.get("w"))[:300])
    p("状态:", json.dumps({k: c.state.get(k) for k in ("account", "five_hour", "weekly")}, ensure_ascii=False)[:600])
    txt = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
    return txt
