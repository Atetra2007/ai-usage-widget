"""切号模块的测试：全部用假令牌和临时目录，不碰真实的 ~/.codex。

运行：.venv\\Scripts\\python -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from providers import codex_switch as sw  # noqa: E402
from providers.codex import AUTH_CLAIM, identity, key_id, parse_workspace_name  # noqa: E402


def jwt(payload):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return enc({"alg": "none"}) + "." + enc(payload) + ".sig"


def auth(email, acct, serial=1):
    """假的 auth.json；serial 模拟 Codex 换新令牌。"""
    claim = {AUTH_CLAIM: {"chatgpt_account_id": acct, "chatgpt_plan_type": "team", "chatgpt_user_id": "u-" + email}}
    return json.dumps({"auth_mode": "chatgpt", "tokens": {
        "id_token": jwt({"email": email, **claim}), "access_token": jwt({"serial": serial, "exp": 4000000000, **claim}),
        "refresh_token": "fake-refresh-%s-%s-%d" % (email, acct, serial), "account_id": acct}}).encode()


A1, A2, B = ("a@x.test", "ws-a1"), ("a@x.test", "ws-a2"), ("b@x.test", "ws-b")


def sid(pair):
    return key_id(identity(json.loads(auth(*pair)))["key"])


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aiw-switch-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "codex")
        os.makedirs(self.home)
        self.auth_path = os.path.join(self.home, "auth.json")
        self.proc_list = []
        self.closed, self.launched = [], []
        self.s = sw.CodexSwitcher(self.home, os.path.join(self.root, "vault"), procs=lambda: list(self.proc_list),
                                  closer=self._close, launcher=lambda: self.launched.append(1))

    def _close(self, pids):
        self.closed.append(list(pids))
        self.proc_list = [p for p in self.proc_list if p[0] not in pids]

    def login(self, pair, serial=1):
        with open(self.auth_path, "wb") as f:
            f.write(auth(*pair, serial))

    def on_disk(self):
        with open(self.auth_path, "rb") as f:
            return f.read()

    def setup_two(self):
        self.login(A1)
        self.s.save_current()
        self.login(B)
        self.s.save_current()


class Dpapi(unittest.TestCase):
    def test_roundtrip(self):
        data = b"secret-token"
        blob = sw.protect(data)
        self.assertNotIn(data, blob)
        self.assertEqual(sw.unprotect(blob), data)

    def test_wrong_blob_rejected(self):
        with self.assertRaises(OSError):
            sw.unprotect(b"not a dpapi blob")


class Classify(unittest.TestCase):
    PKG = "c:\\program files\\windowsapps\\openai.codex_26.1_x64__x\\app\\"

    def test_package_foreign_cockpit(self):
        r = sw.classify([(1, "chatgpt.exe", self.PKG + "chatgpt.exe", 0), (2, "codex.exe", self.PKG + "resources\\codex.exe", 1),
                         (3, "codex.exe", "c:\\npm\\codex.exe", 900), (4, "cockpit-tools.exe", "c:\\x\\cockpit-tools.exe", 0),
                         (5, "codex-windows-sandbox-service.exe", "", 0), (6, "chatgpt.exe", "c:\\other\\chatgpt.exe", 0),
                         (7, "codex.exe", "", 900)])
        self.assertEqual(r["package"], [1, 2])
        self.assertEqual(r["foreign"], [3, 7])   # 读不到路径的 codex 也不能当成没在运行
        self.assertTrue(r["cockpit"])

    def test_desktop_app_server_outside_install_dir(self):
        """真实情况：桌面版的 app-server 和 computer-use 都在 %LOCALAPPDATA%\\OpenAI\\Codex 下，由 ChatGPT.exe 启动。"""
        managed = "c:\\users\\pc\\appdata\\local\\openai\\codex\\"
        r = sw.classify([(1, "chatgpt.exe", self.PKG + "chatgpt.exe", 0),
                         (2, "codex.exe", managed + "bin\\22cf\\codex.exe", 1),
                         (3, "codex-computer-use-swift.exe", managed + "runtimes\\cua\\codex-computer-use-swift.exe", 1),
                         (4, "node.exe", "c:\\users\\pc\\.cache\\codex-runtimes\\node.exe", 1),
                         (5, "extension-host.exe", "c:\\users\\pc\\.codex\\plugins\\extension-host.exe", 800)])
        self.assertEqual(r["package"], [1, 2, 3])
        self.assertEqual(r["foreign"], [])       # 这些都不是用户开的命令行

    def test_leftover_app_server_after_window_closed(self):
        r = sw.classify([(2, "codex.exe", "c:\\users\\pc\\appdata\\local\\openai\\codex\\bin\\22cf\\codex.exe", 1)])
        self.assertEqual(r["package"], [2])      # 窗口关了后台还在：手里有令牌，也要算

    def test_codex_started_inside_the_app_counts_as_app(self):
        r = sw.classify([(1, "chatgpt.exe", self.PKG + "chatgpt.exe", 0), (2, "powershell.exe", "c:\\ps.exe", 1),
                         (3, "codex.exe", "c:\\npm\\codex.exe", 2)])
        self.assertEqual(r["package"], [1, 3])
        self.assertEqual(r["foreign"], [])

    def test_terminal_cli_still_foreign(self):
        r = sw.classify([(1, "chatgpt.exe", self.PKG + "chatgpt.exe", 0), (2, "powershell.exe", "c:\\ps.exe", 800),
                         (3, "codex.exe", "c:\\npm\\codex.exe", 2)])
        self.assertEqual(r["foreign"], [3])      # 用户自己终端里的 CLI 不替他关

    def test_parent_loop_does_not_hang(self):
        r = sw.classify([(3, "codex.exe", "c:\\npm\\codex.exe", 4), (4, "x.exe", "", 3)])
        self.assertEqual(r["foreign"], [3])

    def test_real_process_list_runs(self):
        self.assertTrue(any(p[0] == os.getpid() for p in sw.list_procs()))


class Switching(Base):
    def test_saved_files_are_encrypted(self):
        self.login(A1)
        s = self.s.save_current()
        with open(os.path.join(self.s.dir, s + ".dpapi"), "rb") as f:
            blob = f.read()
        self.assertNotIn(b"fake-refresh", blob)
        self.assertNotIn(b"access_token", blob)
        with open(self.s.meta_path, "rb") as f:
            self.assertNotIn(b"fake-refresh", f.read())

    def test_switch_swaps_and_keeps_rotated_token(self):
        self.setup_two()
        self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), auth(*A1))
        self.login(A1, serial=2)              # Codex 用着用着换了新令牌
        self.s.switch(sid(B))
        self.assertEqual(self.on_disk(), auth(*B))
        self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), auth(*A1, 2))  # 切回来拿到的是新令牌，不是旧的
        self.assertEqual(len(self.launched), 3)

    def test_same_account_two_workspaces_are_separate(self):
        self.login(A1)
        self.s.save_current()
        self.login(A2)
        self.s.save_current()
        self.assertEqual(len(self.s.status()["slots"]), 2)
        self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), auth(*A1))

    def test_running_codex_needs_confirmation(self):
        self.setup_two()
        self.proc_list = [(10, "chatgpt.exe", "c:\\windowsapps\\openai.codex_1\\app\\chatgpt.exe", 0)]
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError) as c:
            self.s.switch(sid(A1))
        self.assertEqual(c.exception.code, "running")
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(self.closed, [])
        self.s.switch(sid(A1), close=True)
        self.assertEqual(self.closed, [[10]])
        self.assertEqual(self.on_disk(), auth(*A1))

    def test_new_child_during_close_is_closed_too(self):
        self.setup_two()
        pkg = "c:\\windowsapps\\openai.codex_1\\app\\chatgpt.exe"
        self.proc_list = [(10, "chatgpt.exe", pkg, 0)]
        rounds = []

        def close(pids):
            rounds.append(list(pids))
            self.proc_list = [(11, "chatgpt.exe", pkg, 0)] if len(rounds) == 1 else []
        self.s.closer = close
        self.s.switch(sid(A1), close=True)
        self.assertEqual(rounds, [[10], [11]])
        self.assertEqual(self.on_disk(), auth(*A1))

    def test_token_rotated_while_closing_is_not_lost(self):
        self.setup_two()
        self.proc_list = [(10, "chatgpt.exe", "c:\\windowsapps\\openai.codex_1\\app\\chatgpt.exe", 0)]

        def close_rotating(pids):
            self.login(B, serial=9)
            self.proc_list = []
        self.s.closer = close_rotating
        self.s.switch(sid(A1), close=True)
        self.s.switch(sid(B))
        self.assertEqual(self.on_disk(), auth(*B, 9))

    def test_blocked_by_cockpit_and_cli(self):
        self.setup_two()
        before = self.on_disk()
        for proc, code in (((1, "cockpit-tools.exe", "c:\\x\\cockpit-tools.exe", 0), "cockpit"),
                           ((2, "codex.exe", "c:\\npm\\codex.exe", 0), "foreign")):
            self.proc_list = [proc]
            with self.assertRaises(sw.SwitchError) as c:
                self.s.switch(sid(A1), close=True)
            self.assertEqual(c.exception.code, code)
            self.assertEqual(self.on_disk(), before)
        self.assertEqual(self.closed, [])

    def test_close_failure_changes_nothing(self):
        self.setup_two()
        self.proc_list = [(10, "chatgpt.exe", "c:\\windowsapps\\openai.codex_1\\app\\chatgpt.exe", 0)]
        self.s.closer = lambda pids: None
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError) as c:
            self.s.switch(sid(A1), close=True)
        self.assertEqual(c.exception.code, "close_failed")
        self.assertEqual(self.on_disk(), before)

    def test_unsaved_login_is_never_overwritten(self):
        self.setup_two()
        self.login(A2)                         # 比如在 Codex 里换了空间，账号和保存的都对不上
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError) as c:
            self.s.switch(sid(A1))
        self.assertEqual(c.exception.code, "unsaved")
        self.assertEqual(self.on_disk(), before)
        self.assertTrue(self.s.status()["unsaved"])

    def test_api_key_login_is_never_overwritten(self):
        self.setup_two()
        with open(self.auth_path, "wb") as f:
            f.write(b'{"auth_mode":"apikey","OPENAI_API_KEY":"fake"}')
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError) as c:
            self.s.switch(sid(A1))
        self.assertEqual(c.exception.code, "auth_unknown")
        self.assertEqual(self.on_disk(), before)

    def test_slot_with_wrong_identity_is_rejected(self):
        self.setup_two()
        shutil.copy(os.path.join(self.s.dir, sid(B) + ".dpapi"), os.path.join(self.s.dir, sid(A1) + ".dpapi"))
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError) as c:
            self.s.switch(sid(A1))
        self.assertEqual(c.exception.code, "slot_unreadable")
        self.assertEqual(self.on_disk(), before)

    def test_corrupt_slot_is_rejected(self):
        self.setup_two()
        with open(os.path.join(self.s.dir, sid(A1) + ".dpapi"), "wb") as f:
            f.write(b"garbage")
        before = self.on_disk()
        with self.assertRaises(sw.SwitchError):
            self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), before)

    def test_add_identity_moves_auth_without_losing_it(self):
        self.login(A1)
        self.s.save_current()
        self.s.begin_login()
        self.assertFalse(os.path.exists(self.auth_path))
        st = self.s.status()
        self.assertTrue(st["no_login"])
        self.login(B)                          # 用户在浏览器里登录了 B
        st = self.s.status()
        self.assertTrue(st["unsaved"])
        self.s.save_current()
        self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), auth(*A1))

    def test_add_identity_refuses_unsaved_current(self):
        self.login(A1)
        with self.assertRaises(sw.SwitchError) as c:
            self.s.begin_login()
        self.assertEqual(c.exception.code, "unsaved")
        self.assertTrue(os.path.exists(self.auth_path))

    def test_cancel_add_by_switching_back(self):
        self.login(A1)
        self.s.save_current()
        self.s.begin_login()
        self.s.switch(sid(A1))
        self.assertEqual(self.on_disk(), auth(*A1))

    def test_backup_restores_and_is_capped(self):
        self.setup_two()
        for n in range(sw.MAX_BACKUPS + 4):
            self.s.switch(sid(A1) if n % 2 == 0 else sid(B))
        files = os.listdir(self.s.backups)
        self.assertLessEqual(len(files), sw.MAX_BACKUPS)
        with open(os.path.join(self.s.backups, sorted(files)[-1]), "rb") as f:
            self.assertTrue(identity(json.loads(sw.unprotect(f.read()))))

    def test_no_temp_files_left(self):
        self.setup_two()
        self.s.switch(sid(A1))
        self.assertEqual(sorted(os.listdir(self.home)), ["auth.json"])

    def test_status_never_contains_tokens(self):
        self.setup_two()
        text = json.dumps(self.s.status())
        for bad in ("fake-refresh", "access_token", "eyJ"):
            self.assertNotIn(bad, text)

    def test_identities_read_from_vault(self):
        self.setup_two()
        keys = {i["key"] for i in self.s.identities()}
        self.assertEqual(len(keys), 2)

    def test_no_rename_api_left(self):
        self.assertFalse(hasattr(self.s, "rename"))


class WorkspaceNames(Base):
    def test_parse_list_and_map_formats(self):
        rows = {"accounts": [{"id": "ws", "name": " 研发组 ", "structure": "workspace"},
                             {"id": "me", "name": None, "structure": "personal"}]}
        self.assertEqual(parse_workspace_name(rows, "ws"), "研发组")
        self.assertIsNone(parse_workspace_name(rows, "me"))
        self.assertIsNone(parse_workspace_name(rows, "missing"))
        as_map = {"accounts": {"ws": {"account": {"account_id": "ws", "name": "研发组", "structure": "workspace"}}}}
        self.assertEqual(parse_workspace_name(as_map, "ws"), "研发组")
        for bad in (None, {}, {"accounts": "x"}, {"accounts": [1]}, {"accounts": {"ws": 1}}):
            self.assertIsNone(parse_workspace_name(bad, "ws"))

    def test_personal_name_is_ignored(self):
        rows = {"accounts": [{"id": "me", "name": "Personal", "structure": "personal"}]}
        self.assertIsNone(parse_workspace_name(rows, "me"))

    def labels(self):
        return {s["id"]: s["label"] for s in self.s.status()["slots"]}

    def test_name_replaces_old_label_and_survives_resave(self):
        self.login(A1)
        s = self.s.save_current()
        self.s.fetch_name = lambda ident: "研发组"
        self.s.refresh_names()
        self.assertEqual(self.labels()[s], "研发组")
        self.s.save_current()
        self.assertEqual(self.labels()[s], "研发组")

    def test_failure_keeps_label_and_retries_later(self):
        self.login(A1)
        s = self.s.save_current()
        before = self.labels()[s]
        calls = []

        def boom(ident):
            calls.append(1)
            raise OSError("offline")
        self.s.fetch_name = boom
        self.s.refresh_names()
        self.s.refresh_names()                 # 马上再调不会重复请求
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.labels()[s], before)
        self.s._name_tried[s] -= sw.NAME_RETRY + 1
        self.s.refresh_names()
        self.assertEqual(len(calls), 2)

    def test_success_is_not_requested_again_until_stale(self):
        self.login(A1)
        s = self.s.save_current()
        calls = []
        self.s.fetch_name = lambda ident: calls.append(1) or "研发组"
        self.s.refresh_names()
        self.s._name_tried.clear()
        self.s.refresh_names()
        self.assertEqual(len(calls), 1)
        meta = self.s._meta()
        meta[s]["named"] -= sw.NAME_EVERY + 1
        self.s._save_meta(meta)
        self.s._name_tried.clear()
        self.s.refresh_names()
        self.assertEqual(len(calls), 2)

    def test_same_workspace_name_on_two_accounts_is_told_apart(self):
        self.setup_two()
        self.s.fetch_name = lambda ident: "团队"
        self.s.refresh_names()
        self.assertEqual(sorted(self.labels().values()), ["团队 · a", "团队 · b"])

    def test_no_name_keeps_label(self):
        self.login(A1)
        s = self.s.save_current()
        before = self.labels()[s]
        self.s.fetch_name = lambda ident: None
        self.s.refresh_names()
        self.assertEqual(self.labels()[s], before)


if __name__ == "__main__":
    unittest.main()
