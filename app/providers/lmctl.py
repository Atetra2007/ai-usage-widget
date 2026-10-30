"""本地模型的手动加载 / 卸载（LM Studio v1 REST API，http://127.0.0.1:1234）。

只给自己用：模型按 key 写死匹配（含 "flash-next"、不含 "mtp"）。
- 加载前先卸载所有已加载的实例（防止 27B 和 Flash-Next 同时在内存里溢出）；
- 卸载 = 卸载全部实例；
- 1234 端口连不上时先 `lms server start` 再重试。
加载请求会一直挂到模型加载完成（90GB 约一分钟），所以放后台线程，界面显示“加载中”。
"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .common import NO_WINDOW

CONTEXT = 65536
LOAD_TIMEOUT = 900      # 秒：首次从硬盘读 90GB 可能比一分钟久
LMS_HOME = os.path.join(os.path.expanduser("~"), ".lmstudio")


def match_key(key):
    k = (key or "").lower()
    return "flash-next" in k and "mtp" not in k


def _port():
    try:
        with open(os.path.join(LMS_HOME, ".internal", "http-server-config.json"), "r", encoding="utf-8") as f:
            return int(json.load(f).get("port") or 1234)
    except Exception:
        return 1234


def _lms_exe():
    p = os.path.join(LMS_HOME, "bin", "lms.exe")
    return p if os.path.exists(p) else "lms"


class LMError(Exception):
    pass


class LMControl:
    def __init__(self, base=None, lms=None):
        self.base = base or f"http://127.0.0.1:{_port()}"
        self.lms = lms or _lms_exe()
        self.lock = threading.Lock()
        self.busy = None          # None / "loading" / "unloading"
        self.busy_since = None
        self.server = False       # REST 接口是否可达
        self.instances = []       # [{"key", "id"}] 当前已加载的 LLM 实例
        self.key = None           # 匹配到的 Flash-Next key
        self.error = None
        self.last_load = None     # 上次加载耗时（秒）
        self._last_poll = 0

    # ---------------------------------------------------------- HTTP
    def _req(self, method, path, body=None, timeout=5):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                msg = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                msg = ""
            if e.code in (401, 403):
                raise LMError("auth")
            raise LMError(f"http {e.code} {msg}".strip())
        return json.loads(raw) if raw.strip() else {}

    def _models(self):
        d = self._req("GET", "/api/v1/models", timeout=4)
        if isinstance(d, list):
            return d
        return d.get("models") or d.get("data") or []

    def _scan(self):
        models = self._models()
        inst, key = [], None
        for m in models:
            k = m.get("key") or m.get("id") or ""
            if m.get("type", "llm") == "llm" and match_key(k) and key is None:
                key = k
            for li in m.get("loaded_instances") or []:
                iid = li.get("id") or li.get("instance_id")
                if iid:
                    inst.append({"key": k, "id": iid})
        self.server, self.instances = True, inst
        if key:
            self.key = key
        return key, inst

    # ---------------------------------------------------------- 状态轮询（采集线程调用）
    def poll(self):
        if self.busy:          # 加载请求挂着时 LM Studio 可能很忙，别打扰
            return
        try:
            self._scan()
        except LMError:
            self.server = True
        except Exception:
            self.server, self.instances = False, []
        self._last_poll = time.time()

    # ---------------------------------------------------------- 服务器
    def _ensure_server(self):
        try:
            return self._scan()
        except LMError:
            raise
        except Exception:
            pass
        try:
            subprocess.run([self.lms, "server", "start"], creationflags=NO_WINDOW,
                           capture_output=True, timeout=60)
        except FileNotFoundError:
            raise LMError("no_lms")
        except subprocess.TimeoutExpired:
            pass
        t0 = time.time()
        while time.time() - t0 < 45:
            try:
                return self._scan()
            except LMError:
                raise
            except Exception:
                time.sleep(1)
        raise LMError("server_down")

    def _unload_all(self, inst):
        for i in inst:
            try:
                self._req("POST", "/api/v1/models/unload", {"instance_id": i["id"]}, timeout=60)
            except LMError as e:
                if "404" not in str(e):  # 已经被别处卸载了就算了
                    raise

    # ---------------------------------------------------------- 动作（界面调用）
    def _run(self, kind, fn):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.busy_since, self.error = kind, time.time(), None

        def work():
            try:
                fn()
            except LMError as e:
                self.error = str(e)
            except Exception as e:  # noqa
                self.error = f"{type(e).__name__}: {e}"[:160]
            finally:
                self.busy = None
                try:
                    self._scan()
                except Exception:
                    self.server, self.instances = False, []
        threading.Thread(target=work, daemon=True).start()
        return True

    def load(self):
        def fn():
            key, inst = self._ensure_server()
            if not key:
                raise LMError("no_model")
            self._unload_all(inst)
            t0 = time.time()
            r = self._req("POST", "/api/v1/models/load",
                          {"model": key, "context_length": CONTEXT, "offload_kv_cache_to_gpu": True},
                          timeout=LOAD_TIMEOUT)
            self.last_load = round(float(r.get("load_time_seconds") or (time.time() - t0)), 1)
        return self._run("loading", fn)

    def unload(self):
        def fn():
            try:
                _, inst = self._scan()
            except LMError:
                raise
            except Exception:
                return  # 服务器没开 = 本来就没有加载
            self._unload_all(inst)
        return self._run("unloading", fn)

    def build(self):
        inst = self.instances
        mine = [i for i in inst if match_key(i["key"])]
        if self.busy:
            state = self.busy
        elif mine:
            state = "loaded"
        elif inst:
            state = "other"
        else:
            state = "unloaded"
        return {"state": state, "server": self.server, "key": self.key,
                "loaded": [i["key"] for i in inst],
                "elapsed": round(time.time() - self.busy_since) if self.busy and self.busy_since else None,
                "last_load": self.last_load, "error": self.error}
