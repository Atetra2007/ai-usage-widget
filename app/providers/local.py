"""本地模型：解析 llama.cpp 服务器日志（Bionic / LM Studio），并尝试 /slots 读实时状态。"""
import glob
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime

from .common import FileCache, History, local_date, now, parse_iso, today, window_info  # noqa: F401

RE_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")
RE_MODEL = re.compile(r"loading model '([^']+)'")
RE_CTX = re.compile(r"n_ctx_slot = (\d+)")
RE_PORT = re.compile(r"listening on http://127\.0\.0\.1:(\d+)")
RE_PROMPT = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
RE_EVAL = re.compile(r"\|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
RE_DRAFT = re.compile(r"draft acceptance = ([\d.]+)")


def default_dirs():
    lm = os.path.join(os.path.expanduser("~"), ".lmstudio")
    return [("Bionic", os.path.join(lm, "apps", "bionic", "server-logs")),
            ("LM Studio", os.path.join(lm, "server-logs"))]


def detect(extra_dirs=()):
    """找 Bionic / LM Studio / 自定义 llama.cpp 日志目录。"""
    found = []
    for name, d in default_dirs() + [("llama.cpp", x) for x in extra_dirs if x]:
        if glob.glob(os.path.join(d, "*.log")) or glob.glob(os.path.join(d, "*", "*.log")):
            found.append({"name": name, "path": d})
    return {"status": "ok" if found else "missing", "sources": found}


class LocalCollector:
    """解析 llama.cpp 服务器日志（Bionic / LM Studio），并尝试读 /slots 拿实时速度。"""

    def __init__(self, log_dirs, cache, history):
        self.dirs = [d for d in log_dirs if d]
        self.cache = cache
        self.history = history
        self.cur_file = None
        self.offset = 0
        self.ts = None
        self.model = None
        self.ctx = None
        self.port = None
        self.loaded = False
        self.generating = False
        self.gen_since = None
        self.pending_prompt = 0
        self.last = None  # 最近一次完成的回复
        self.speeds = []  # 最近的完成速度
        self.live = None
        self._slot_prev = None
        self._slots_ok = False
        self._slots_port = None
        self.slots_status = "unknown"
        self.slot_busy = False
        self.slot_decoded = 0
        self._gen_seen = 0
        self.is_gen = False
        self.cur = 0.0
        self.estimated = False
        from collections import deque
        self.timeline = deque([0.0] * 90, maxlen=90)  # 最近 90 秒的速度
        self._last_full = 0

    def _files(self):
        out = []
        for d in self.dirs:
            out += glob.glob(os.path.join(d, "*", "*.log")) + glob.glob(os.path.join(d, "*.log"))
        return sorted(set(out), key=lambda p: (os.path.basename(p), p))

    def _line(self, line, daily=None, live=True):
        m = RE_TS.match(line)
        if m:
            try:
                self.ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                pass
        if "loading model" in line:
            m = RE_MODEL.search(line)
            if m and live:
                self.model = re.split(r"[\\/]", m.group(1))[-1].replace(".gguf", "")
                self.loaded = True
        elif "n_ctx_slot" in line and live:
            m = RE_CTX.search(line)
            if m:
                self.ctx = int(m.group(1))
        elif "listening on" in line and live:
            m = RE_PORT.search(line)
            if m:
                self.port = int(m.group(1))
        elif "processing task" in line and live:
            self.generating = True
            self.gen_since = self.ts
        elif "prompt eval time" in line:
            m = RE_PROMPT.search(line)
            if m:
                self.pending_prompt = int(m.group(2))
        elif "eval time" in line:
            m = RE_EVAL.search(line)
            if m:
                gen = int(m.group(2))
                spd = float(m.group(3))
                day = local_date(self.ts) if self.ts else today()
                if daily is not None:
                    d = daily[day]
                    d["total"] += gen + self.pending_prompt
                    d["output"] += gen
                    d["count"] += 1
                if live:
                    self.last = {"speed": round(spd, 1), "tokens": gen, "prompt": self.pending_prompt,
                                 "ts": self.ts, "draft": None}
                    self.speeds = (self.speeds + [round(spd, 1)])[-40:]
                self.pending_prompt = 0
        elif "draft acceptance" in line and live and self.last:
            m = RE_DRAFT.search(line)
            if m:
                self.last["draft"] = round(float(m.group(1)) * 100)
        elif ("stop processing" in line or "release:" in line) and live:
            self.generating = False
            self.live = None
        elif "Server stopped" in line and live:
            self.loaded = False
            self.generating = False

    def full_scan(self):
        """统计全部历史日志的每日 token（带缓存），并定位要实时跟踪的最新文件。"""
        files = self._files()
        merged = defaultdict(lambda: {"total": 0, "output": 0, "count": 0})
        for fp in files:
            try:
                st = os.stat(fp)
            except OSError:
                continue
            v = self.cache.get("local:" + fp, st)
            if v is None:
                daily = defaultdict(lambda: {"total": 0, "output": 0, "count": 0})
                saved = (self.ts, self.pending_prompt)
                self.ts, self.pending_prompt = None, 0
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            self._line(line, daily, live=False)
                except Exception:
                    pass
                self.ts, self.pending_prompt = saved
                v = dict(daily)
                self.cache.put("local:" + fp, st, v)
            for day, vals in v.items():
                for k in ("total", "output", "count"):
                    merged[day][k] += vals.get(k, 0)
        self.cache.prune("local:", {"local:" + fp for fp in files})
        self.history.merge("local", merged)
        if files and files[-1] != self.cur_file:
            self.cur_file = files[-1]
            self.offset = 0
            self.generating = False
            # 先快速回放整份最新日志，恢复模型/端口/最近速度等状态
            self._tail(replay=True)
        self._last_full = now()

    def _tail(self, replay=False):
        if not self.cur_file:
            return
        try:
            size = os.path.getsize(self.cur_file)
            if size < self.offset:
                self.offset = 0
            if size == self.offset:
                return
            with open(self.cur_file, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
            nl = chunk.rfind(b"\n")
            if nl < 0:
                return
            self.offset += nl + 1
            for line in chunk[:nl + 1].decode("utf-8", errors="replace").splitlines():
                self._line(line, None, live=True)
            if replay and self.generating and self.ts and now() - self.ts > 600:
                self.generating = False
        except OSError:
            pass

    def _poll_slots(self):
        """读 llama-server 的 /slots：能用就拿真实的实时速度和生成状态。"""
        if not self.port:
            self.slots_status = "no-port"
            return
        if self._slots_port == self.port and not self._slots_ok:
            return  # 这个端口已确认不可用
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/slots", timeout=0.4) as r:
                slots = json.loads(r.read().decode())
            if not isinstance(slots, list):
                raise ValueError("not a list")
            self._slots_port, self._slots_ok, self.slots_status = self.port, True, "ok"
        except urllib.error.HTTPError as e:
            self._slots_port, self._slots_ok, self.slots_status = self.port, False, f"http {e.code}"
            return
        except Exception as e:  # 服务器没开 / 端口过期
            self.slots_status = f"err {type(e).__name__}"
            self._slot_prev = None
            return
        n, busy = 0, False
        for sl in slots:
            if sl.get("is_processing"):
                busy = True
                nt = sl.get("next_token")
                if isinstance(nt, list):
                    nt = nt[0] if nt else {}
                n += int((nt or {}).get("n_decoded", 0) or 0)
        self.slot_busy = busy
        self.slot_decoded = n if busy else 0
        t = now()
        if busy and self._slot_prev and n >= self._slot_prev[1] and t > self._slot_prev[0]:
            rate = (n - self._slot_prev[1]) / (t - self._slot_prev[0])
            self.live = round(rate if self.live is None else self.live * 0.4 + rate * 0.6, 1)
        elif not busy:
            self.live = None
        self._slot_prev = (t, n) if busy else None

    def _estimate(self):
        """没有 /slots 时，生成中用最近几次的平均速度作为估算。"""
        recent = self.speeds[-5:]
        return round(sum(recent) / len(recent), 1) if recent else 0.0

    def tick(self):
        if now() - self._last_full > 20:
            self.full_scan()
        self._tail()
        self._poll_slots()
        t = now()
        if self._slots_ok and self._slots_port == self.port:
            gen = self.slot_busy
            cur = self.live or 0.0
            self.estimated = False
        else:
            gen = self.generating
            cur = self._estimate() if gen else 0.0
            self.estimated = gen
        if gen:
            self._gen_seen = t
        elif t - self._gen_seen < 2.0:  # 多步任务之间的短暂空档不归零，避免闪烁
            gen, cur = True, (self.timeline[-1] if self.timeline else 0.0)
        self.is_gen = gen
        self.cur = round(cur, 1)
        self.timeline.append(self.cur)

    def build(self, gpu=None):
        td, tt = self.history.totals("local")
        to, _ = self.history.totals("local", "output")
        live_out = self.slot_decoded if self.is_gen else 0  # 正在生成的这条也算进今日
        return {"ok": bool(self.cur_file), "model": self.model, "ctx": self.ctx, "loaded": self.loaded,
                "generating": self.is_gen, "speed": self.cur, "estimated": self.estimated,
                "last": self.last, "timeline": list(self.timeline),
                "today": td + live_out, "today_output": to + live_out, "total": tt + live_out,
                "slots": self.slots_status, "gpu": gpu, "week": self.history.last_days("local")}
