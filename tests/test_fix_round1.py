"""Regression tests for review round 1 (findings 1-11). One class per finding.

Each test fails on the pre-review build and passes on the fix. To run them against another
build of the script:  CLAUDE_SEATS_SCRIPT=/path/to/old/claude-seats python3 -m unittest tests.test_fix_round1
Nothing here touches the network, the Keychain, a real config dir or a real `claude`.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPT = os.environ.get("CLAUDE_SEATS_SCRIPT") or os.path.join(os.path.dirname(HERE), "bin", "claude-seats")

TMP_ROOT = tempfile.mkdtemp(prefix="claude-seats-r1-root-")
os.environ["CLAUDE_SEATS_ROOT"] = TMP_ROOT
for k in ("CLAUDE_SEATS_HANDOFF", "CLAUDE_SEATS_HANDOFF_AT", "CC_HANDOFF", "CC_HANDOFF_AT", "CLAUDE_SEATS_INSIDE",
          "CLAUDE_SEATS_FORCE_TARGET", "CLAUDE_SEATS_FORCE_LIMIT"):
    os.environ.pop(k, None)


def load_module():
    loader = importlib.machinery.SourceFileLoader("claude_seats_r1", SCRIPT)
    spec = importlib.util.spec_from_loader("claude_seats_r1", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cs = load_module()


def fixture(name):
    return json.load(open(os.path.join(FIX, name)))


def usage_ok(name):
    return {"ok": True, "source": "test", "data": fixture(name)}


def seat(sid, usable=True, usage=None, marked_until=None):
    return {"id": sid, "usable": usable, "usage": usage, "marked_until": marked_until}


def fake_registry(root, seats=("a", "b")):
    r = {"primary": seats[0], "accounts": [], "share": cs.DEFAULT_SHARE}
    for s in seats:
        d = os.path.join(root, "seat-" + s)
        os.makedirs(d, exist_ok=True)
        r["accounts"].append({"id": s, "label": s, "configDir": d, "dir": d, "json": os.path.join(d, ".claude.json")})
    return r


class F1_HandoffHandshake(unittest.TestCase):
    """A recorded Stop event is not permission to kill. The hook waits; the supervisor ends the
    child only while that hook is still waiting; a turn that started meanwhile is left alone."""

    def test_hook_still_waiting_rules(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "req.json")
        now = time.time()
        req = {"token": "t", "hook_pid": os.getpid(), "expires_at": now + 5}
        open(p, "w").write("{}")
        self.assertTrue(cs.hook_still_waiting(req, p, now=now))
        self.assertFalse(cs.hook_still_waiting(dict(req, expires_at=now + 0.2), p, now=now))   # about to fail open
        self.assertFalse(cs.hook_still_waiting(dict(req, hook_pid=2 ** 22 - 1), p, now=now))   # hook gone
        os.remove(p)
        self.assertFalse(cs.hook_still_waiting(req, p, now=now))                               # withdrawn
        shutil.rmtree(d)

    def test_hook_waits_until_released_and_fails_open_on_timeout(self):
        run_id = "run-f1"
        req_path, dec_path, _ = cs.hook_paths(run_id)
        os.makedirs(os.path.dirname(req_path), exist_ok=True)
        cs.write_json(req_path, {"token": "tok"})
        result = {}
        t0 = time.time()
        th = threading.Thread(target=lambda: result.update(r=cs.await_decision(run_id, "tok", time.time() + 5, os.getpid())))
        th.start()
        time.sleep(0.4)
        self.assertTrue(th.is_alive(), "hook returned before any decision")
        cs.write_json(dec_path, {"token": "tok", "action": "stay"})
        th.join(3)
        self.assertEqual(result.get("r"), "stay")
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(os.path.exists(req_path) or os.path.exists(dec_path))
        # fail-open: no decision -> the hook withdraws its request and returns
        cs.write_json(req_path, {"token": "tok2"})
        r = cs.await_decision(run_id, "tok2", time.time() + 0.6, os.getpid())
        self.assertEqual(r, "timeout")
        self.assertFalse(os.path.exists(req_path))

    def test_second_turn_during_decision_window_is_not_killed(self):
        """The hook requests a handoff, the supervisor deliberates (slow pick), meanwhile the hook
        fails open and a new turn starts (request withdrawn). The child must survive."""
        root = TMP_ROOT
        r = fake_registry(root)
        sup = cs.Supervisor(r, ["--model", "haiku"])          # interactive
        sup.handoff = True
        a = r["accounts"][0]
        calls = {"restore": 0}

        def fake_spawn(seat_, resume=False, continue_turn=False):
            sup.child = subprocess.Popen(["sleep", "30"])
            sup.generation = 1
            os.makedirs(os.path.dirname(sup.since), exist_ok=True)
            open(sup.since, "w").write("%f" % time.time())
            sup.killed_by_us = False
            # the hook writes its request right after this turn ends...
            req = {"ts": time.time(), "event": "Stop", "seat": "a", "session_id": "s1", "reason": "threshold",
                   "detail": "99% (5-hour) >= 95%", "pct": 99, "bucket": "5-hour", "token": "tok",
                   "hook_pid": os.getpid(), "run_id": sup.run_id, "gen": "1", "expires_at": time.time() + 0.6}
            cs.write_json(sup.events, req)

        def slow_pick(current=None, spent=(), fresh=False):
            time.sleep(1.0)      # ...the supervisor is slow; the hook times out and withdraws (new turn starts)
            try:
                os.remove(sup.events)
            except OSError:
                pass
            return "b", [{"id": "a", "pct": 99, "bucket": "5-hour", "resets_at": None, "state": "over threshold"},
                         {"id": "b", "pct": 10, "bucket": "5-hour", "resets_at": None, "state": "ok"}]

        sup.spawn = fake_spawn
        sup.pick = slow_pick
        sup.first_seat = lambda: (a, None)
        sup._prefetch_loop = lambda: None
        sup.restore_terminal = lambda: calls.__setitem__("restore", calls["restore"] + 1)
        saved_login_state = cs.login_state
        cs.login_state = lambda *_a, **_k: "ok"

        def user_quits():
            time.sleep(2.5)
            if sup.child and sup.child.poll() is None:
                sup.child.kill()   # the user ends the session (SIGKILL, distinct from our SIGTERM)
        threading.Thread(target=user_quits, daemon=True).start()
        old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = sup.run()
        finally:
            for s, h in old.items():
                signal.signal(s, h)
            cs.login_state = saved_login_state
        self.assertEqual(sup.handoffs, 0, "supervisor handed off although the hook had released the turn")
        self.assertEqual(rc, -9, "child was ended by the supervisor (SIGTERM) instead of surviving until the user quit")
        self.assertFalse(sup.killed_by_us)
        self.assertGreaterEqual(calls["restore"], 1, "terminal not restored on supervisor exit")


class F2_EqualsFormOptions(unittest.TestCase):
    def test_equals_form_streaming_flags_disable_buffering(self):
        argv = ["-p", "--input-format=stream-json", "--output-format=stream-json", "--verbose"]
        info = cs.parse_args(cs.normalize_argv(argv))
        self.assertTrue(info["stream"])
        r = fake_registry(TMP_ROOT)
        sup = cs.Supervisor(r, argv)
        self.assertFalse(sup.buffer_stdout)
        info = cs.parse_args(cs.normalize_argv(["--model=sonnet", "--settings=/tmp/x.json", "hi"]))
        self.assertEqual((info["model"], info["settings"], info["prompt_idx"]), ("sonnet", "/tmp/x.json", 4))


class F3_ShapeValidation(unittest.TestCase):
    def test_invalid_settings_refuse_to_launch(self):
        with self.assertRaises(cs.LaunchError):
            cs.load_settings_arg("[]")
        with self.assertRaises(cs.LaunchError):
            cs.merged_settings({"hooks": "nope"}, "h")
        with self.assertRaises(cs.LaunchError):
            cs.merged_settings({"hooks": {"Stop": "nope"}}, "h")
        with self.assertRaises(cs.LaunchError):
            cs.merged_settings({"hooks": {"Stop": [1, 2]}}, "h")
        with self.assertRaises(cs.LaunchError):
            cs.Supervisor(fake_registry(TMP_ROOT), ["--settings", "[]"])
        # a caller's permissions survive untouched
        caller = {"permissions": {"deny": ["Bash(rm:*)"], "defaultMode": "plan"}, "hooks": None}
        m = cs.merged_settings(caller, "h")
        self.assertEqual(m["permissions"], caller["permissions"])
        m = cs.merged_settings({"hooks": {"Stop": None}}, "h")
        self.assertEqual(len(m["hooks"]["Stop"]), 1)

    def test_unexpected_usage_shapes_are_unavailable_not_crashes(self):
        self.assertIsNone(cs.limit_report({"ok": True, "data": []}))
        self.assertIsNone(cs.limit_report({"ok": True, "data": {"limits": "x"}}))
        self.assertIsNone(cs.limit_report({"ok": True, "data": {"limits": {"kind": "session"}}}))
        rep = cs.limit_report({"ok": True, "data": {"limits": [
            "junk", {"kind": "session", "percent": "42"}, {"kind": "session", "percent": True},
            {"kind": "weekly_all", "percent": 30, "scope": "bad", "resets_at": 5},
            {"kind": "weekly_scoped", "percent": 10, "scope": {"model": "bad"}}]}})
        self.assertIsNone(rep["session"])
        self.assertEqual(rep["overall"]["pct"], 30)
        self.assertIsNone(rep["overall"]["resets_at"])
        self.assertIsNone(rep["scoped"])
        chosen, rows = cs.choose_seat([seat("a", usage={"ok": True, "data": None}), seat("b", usage=usage_ok("usage-normal.json"))])
        self.assertEqual(chosen, "b")
        self.assertEqual(rows[0]["state"], "usage unknown")


class F4_OfficialBinary(unittest.TestCase):
    def test_find_claude_skips_this_launcher_and_shims_to_it(self):
        d = tempfile.mkdtemp()
        me = os.path.join(d, "launcher")
        open(me, "w").write("#!/bin/sh\n"); os.chmod(me, 0o755)
        shimdir, realdir = os.path.join(d, "shim"), os.path.join(d, "real")
        os.makedirs(shimdir); os.makedirs(realdir)
        os.symlink(me, os.path.join(shimdir, "claude"))                    # PATH entry named claude -> us
        real = os.path.join(realdir, "claude")
        open(real, "w").write("#!/bin/sh\n"); os.chmod(real, 0o755)
        self.assertEqual(cs.find_claude(path_env=shimdir + os.pathsep + realdir, me=me), os.path.realpath(real))
        with self.assertRaises(cs.LaunchError):
            cs.find_claude(path_env=shimdir, me=me)
        shutil.rmtree(d)

    def test_recursion_guard_refuses_before_touching_anything(self):
        env = dict(os.environ, CLAUDE_SEATS_INSIDE="run-x", CLAUDE_SEATS_ROOT=tempfile.mkdtemp())
        out = subprocess.run([sys.executable, SCRIPT, "-p", "hello"], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("refusing", out.stderr + out.stdout)

    def test_spawn_and_refresh_use_the_resolved_absolute_binary(self):
        cs._CLAUDE_BIN = "/opt/fake/claude"
        try:
            self.assertEqual(cs.refresh_command()[0], "/opt/fake/claude")
            sup = cs.Supervisor(fake_registry(TMP_ROOT), ["-p", "x"])
            sup.handoff = False
            self.assertEqual(sup.build_cmd(False, False)[0], "/opt/fake/claude")
        finally:
            cs._CLAUDE_BIN = None


class F5_TerminalRestore(unittest.TestCase):
    def test_sigkill_fallback_restores_terminal(self):
        sup = cs.Supervisor(fake_registry(TMP_ROOT), [])
        calls = []
        sup.restore_terminal = lambda: calls.append(1)
        sup.child = subprocess.Popen([sys.executable, "-c",
                                      "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
        time.sleep(0.5)
        t0 = time.time()
        sup.end_child(grace=0.5)
        self.assertIsNotNone(sup.child.poll())
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(calls, [1])
        self.assertIn("\x1b[?25h", cs.TERMINAL_RESET)
        self.assertIn("\x1b[?2004l", cs.TERMINAL_RESET)


class F6_RefreshProbe(unittest.TestCase):
    def test_probe_never_persists_a_session(self):
        cs._CLAUDE_BIN = "/opt/fake/claude"
        try:
            self.assertIn("--no-session-persistence", cs.refresh_command())
        finally:
            cs._CLAUDE_BIN = None

    def test_probe_runs_in_a_private_temp_dir(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cwd"] = kw.get("cwd"); seen["cmd"] = cmd
            class R: returncode = 0
            return R()
        orig = cs.subprocess.run
        cs.subprocess.run = fake_run
        cs._CLAUDE_BIN = "/opt/fake/claude"
        try:
            a = {"id": "z", "dir": "/tmp/z", "default": False}
            os.environ.pop("CLAUDE_SEATS_REFRESH", None)
            self.assertTrue(cs.refresh_seat(a, "test"))
        finally:
            cs.subprocess.run = orig
            cs._CLAUDE_BIN = None
        self.assertIn("claude-seats-refresh-", seen["cwd"])
        self.assertNotEqual(os.path.realpath(seen["cwd"]), os.path.realpath(os.getcwd()))
        self.assertFalse(os.path.exists(seen["cwd"]))  # cleaned up
        self.assertIn("--no-session-persistence", seen["cmd"])


class F7_StructuredLimitRecords(unittest.TestCase):
    def test_quoted_limit_text_in_a_user_prompt_is_not_a_limit(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.jsonl")
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 10))
        recs = [
            {"type": "user", "timestamp": stamp, "message": {"role": "user",
             "content": 'Why does my log say "Usage limit reached" and rate_limit_reached? Explain usage_limit_reached.'}},
            {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Usage limit reached means the 5-hour bucket is full; hit your limit ..."}]}},
            {"type": "system", "subtype": "turn_duration", "timestamp": stamp, "content": "Usage limit reached"},
        ]
        with open(path, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        self.assertIsNone(cs.transcript_limit_hit(path, now - 1800))
        payload = dict(fixture("stop-payload.json"), transcript_path=path)
        self.assertIsNone(cs.evaluate_turn(payload, "a", usage_ok("usage-normal.json"), 95, True, now=now))
        # the real native records still count
        with open(path, "a") as f:
            for line in open(os.path.join(FIX, "transcript-usage-limit.jsonl")):
                rec = json.loads(line); rec["timestamp"] = stamp
                f.write(json.dumps(rec) + "\n")
        hit = cs.transcript_limit_hit(path, now - 1800)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["type"], "system")
        shutil.rmtree(d)


class F8_OptionArities(unittest.TestCase):
    def test_prompt_before_variadic_option_keeps_every_value(self):
        argv = cs.normalize_argv(["Fix bug", "--add-dir", "../lib", "../tests"])
        info = cs.parse_args(argv)
        self.assertEqual(info["prompt_idx"], 0)
        self.assertIsNone(info["ambiguous"])
        self.assertEqual(cs.resume_args(argv, "S", False), ["--add-dir", "../lib", "../tests", "--resume", "S"])
        argv = ["Fix bug", "--disallowedTools", "Bash", "Edit"]
        self.assertEqual(cs.resume_args(argv, "S", True), ["--disallowedTools", "Bash", "Edit", "--resume", "S", cs.CONTINUE_PROMPT])

    def test_ambiguous_arguments_turn_rotation_off_instead_of_guessing(self):
        info = cs.parse_args(["--add-dir", "../lib", "Fix bug"])
        self.assertIsNotNone(info["ambiguous"])
        sup = cs.Supervisor(fake_registry(TMP_ROOT), ["--add-dir", "../lib", "Fix bug"])
        self.assertFalse(sup.handoff)
        self.assertIn("--add-dir", sup.handoff_off_reason)
        info = cs.parse_args(["--made-up-flag", "x", "prompt"])
        self.assertIn("unknown option", info["ambiguous"])
        info = cs.parse_args(["one", "two"])
        self.assertIn("more than one positional", info["ambiguous"])
        # explicit separator removes the ambiguity
        info = cs.parse_args(["--add-dir", "../lib", "--", "Fix bug"])
        self.assertIsNone(info["ambiguous"])
        self.assertEqual(info["prompt_idx"], 3)
        self.assertEqual(cs.resume_args(["--add-dir", "../lib", "--", "Fix bug"], "S", False), ["--add-dir", "../lib", "--resume", "S"])

    def test_optional_value_options(self):
        info = cs.parse_args(["--resume", "-p", "q"])
        self.assertEqual((info["resume"], info["print"], info["prompt_idx"]), ("", True, 2))
        info = cs.parse_args(["-r", "abc", "--debug", "-p", "q"])
        self.assertEqual((info["resume"], info["prompt_idx"]), ("abc", 4))
        self.assertEqual(cs.resume_args(["-r", "abc", "--debug", "-p", "q"], "S", False), ["--debug", "-p", "--resume", "S"])


class F9_PerBucketLimits(unittest.TestCase):
    def setUp(self):
        cs.save_state({"limits": {}})

    def test_each_exhausted_bucket_keeps_its_own_reset(self):
        infos = [seat("a", usage=usage_ok("usage-session-spent.json"))]
        cs.record_blocked(None, infos, want_scoped=True)
        lim = cs.load_state()["limits"]["a"]
        self.assertIn("session", lim)
        self.assertNotIn("overall", lim)
        self.assertNotIn("all", lim)
        self.assertAlmostEqual(lim["session"]["until"], cs.parse_ts("2026-10-28T19:40:00.044249+00:00"), delta=1)
        self.assertNotAlmostEqual(lim["session"]["until"], cs.parse_ts("2026-10-30T23:00:00.044268+00:00"), delta=1)

    def test_scoped_limit_only_blocks_the_scoped_model(self):
        cs.mark("a", "scoped", until=time.time() + 3600)
        self.assertIsNotNone(cs.limited("a", want_scoped=True))
        self.assertIsNone(cs.limited("a", want_scoped=False))
        cs.mark("b", "session", until=time.time() + 3600)
        self.assertIsNotNone(cs.limited("b", want_scoped=False))


class F10_AuthFailureAfterRefresh(unittest.TestCase):
    def test_definite_auth_failure_is_never_unknown_50(self):
        worse = usage_ok("usage-normal.json")
        for l in worse["data"]["limits"]:
            l["percent"] = 80
        chosen, rows = cs.choose_seat([seat("a", usage={"ok": False, "auth_failed": True, "reason": "HTTP 401"}),
                                       seat("b", usage=worse)])
        self.assertEqual(chosen, "b")
        self.assertEqual(rows[0]["state"], "auth failed")
        # network trouble (no auth verdict) still ranks as unknown
        chosen, _ = cs.choose_seat([seat("a", usage={"ok": False, "reason": "timeout"}), seat("b", usage=worse)])
        self.assertEqual(chosen, "a")

    def test_seat_infos_rechecks_login_after_the_refresh(self):
        r = fake_registry(TMP_ROOT)
        for a in r["accounts"]:
            open(a["json"], "w").write("{}")
        states = {"n": 0}

        def flipping_login_state(a, creds=None, now=None):
            states["n"] += 1
            return "stale" if states["n"] <= 2 else "expired"   # usable before the probe, logged out after
        orig_ls, orig_cu = cs.login_state, cs.cached_usage
        cs.login_state = flipping_login_state
        cs.cached_usage = lambda a, ttl=None: {"ok": False, "auth_failed": True, "reason": "refresh failed"}
        try:
            infos = cs.seat_infos({"accounts": r["accounts"][:1], "primary": "a", "share": cs.DEFAULT_SHARE})
        finally:
            cs.login_state, cs.cached_usage = orig_ls, orig_cu
        self.assertFalse(infos[0]["usable"])
        chosen, rows = cs.choose_seat(infos)
        self.assertIsNone(chosen)


class F11_AddTouchesOnlyTheNewSeat(unittest.TestCase):
    def test_add_does_not_relink_or_resync_other_seats(self):
        root = tempfile.mkdtemp(prefix="claude-seats-r1-add-")
        home = tempfile.mkdtemp(prefix="claude-seats-r1-home-")
        old = dict(os.environ)
        os.environ["CLAUDE_SEATS_ROOT"] = root
        os.environ["HOME"] = home
        mod = load_module()
        try:
            prim, other, new = [os.path.join(home, x) for x in ("p", "b", "c")]
            os.makedirs(prim); os.makedirs(other)
            open(os.path.join(prim, "settings.json"), "w").write("{}")
            json.dump({"mcpServers": {"x": {}}}, open(os.path.join(prim, ".claude.json"), "w"))
            os.symlink("/stale/target/settings.json", os.path.join(other, "settings.json"))   # b: stale link
            json.dump({"mcpServers": {"old": {}}}, open(os.path.join(other, ".claude.json"), "w"))  # b: out of sync
            reg = {"primary": "p", "accounts": [{"id": "p", "label": "p", "configDir": prim},
                                                {"id": "b", "label": "b", "configDir": other}], "share": mod.DEFAULT_SHARE}
            json.dump(reg, open(os.path.join(root, "accounts.json"), "w"))
            with contextlib.redirect_stdout(io.StringIO()):
                mod.cmd_add(mod.reg(), ["c", new])
            self.assertEqual(os.readlink(os.path.join(other, "settings.json")), "/stale/target/settings.json")
            self.assertEqual(json.load(open(os.path.join(other, ".claude.json"))), {"mcpServers": {"old": {}}})
            self.assertEqual(os.readlink(os.path.join(new, "settings.json")), os.path.join(prim, "settings.json"))
            self.assertEqual(json.load(open(os.path.join(new, ".claude.json"))), {"mcpServers": {"x": {}}})
        finally:
            os.environ.clear(); os.environ.update(old)
            shutil.rmtree(root); shutil.rmtree(home)


if __name__ == "__main__":
    unittest.main()
