"""Regression tests for review round 2 (items A-E). Each fails on 4339917 and passes on the fix.

    CLAUDE_SEATS_SCRIPT=/path/to/old/claude-seats python3 -m unittest tests.test_fix_round2
"""
import contextlib
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

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPT = os.environ.get("CLAUDE_SEATS_SCRIPT") or os.path.join(os.path.dirname(HERE), "bin", "claude-seats")
TMP_ROOT = tempfile.mkdtemp(prefix="claude-seats-r2-root-")
os.environ["CLAUDE_SEATS_ROOT"] = TMP_ROOT
for k in ("CLAUDE_SEATS_HANDOFF", "CLAUDE_SEATS_HANDOFF_AT", "CC_HANDOFF", "CC_HANDOFF_AT", "CLAUDE_SEATS_INSIDE",
          "CLAUDE_SEATS_FORCE_TARGET", "CLAUDE_SEATS_FORCE_LIMIT"):
    os.environ.pop(k, None)


def load_module():
    loader = importlib.machinery.SourceFileLoader("claude_seats_r2", SCRIPT)
    spec = importlib.util.spec_from_loader("claude_seats_r2", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cs = load_module()


def fixture(name):
    return json.load(open(os.path.join(FIX, name)))


def usage_ok(name, future=False):
    d = fixture(name)
    if future:  # the fixtures' reset times are fixed dates; push them ahead so marks are "still blocking"
        for l in d["limits"]:
            l["resets_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600 * (1 + l["percent"] % 7)))
    return {"ok": True, "source": "test", "data": d}


def fake_registry(root, seats=("a", "b")):
    r = {"primary": seats[0], "accounts": [], "share": cs.DEFAULT_SHARE}
    for s in seats:
        d = os.path.join(root, "seat-" + s)
        os.makedirs(d, exist_ok=True)
        r["accounts"].append({"id": s, "label": s, "configDir": d, "dir": d, "json": os.path.join(d, ".claude.json")})
    return r


def passthrough_cmd(argv):
    """The child command for argv with rotation decided by the Supervisor itself."""
    cs._CLAUDE_BIN = "/opt/fake/claude"
    try:
        sup = cs.Supervisor(fake_registry(TMP_ROOT), argv)
        cmd = sup.build_cmd(False, False)
    finally:
        cs._CLAUDE_BIN = None
    return sup, cmd


class A_HeadlessPassThrough(unittest.TestCase):
    """-p with stream-json input/output or --no-session-persistence: plain child, unbuffered, no hooks, no rerun."""

    def check(self, argv, reason_fragment):
        sup, cmd = passthrough_cmd(argv)
        self.assertFalse(sup.handoff, argv)
        self.assertFalse(sup.buffer_stdout, argv)
        self.assertIn(reason_fragment, sup.handoff_off_reason or "")
        self.assertEqual(cmd[1:], argv, "child argv must be exactly the caller's: no --settings overlay, no --session-id")

    def test_input_stream_json(self):
        self.check(["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"], "stream-json")

    def test_output_stream_json_equals_form(self):
        self.check(["-p", "--output-format=stream-json", "--verbose", "hello"], "stream-json")

    def test_no_session_persistence(self):
        self.check(["-p", "--no-session-persistence", "hello"], "no-session-persistence")

    def test_plain_headless_still_rotates(self):
        sup, cmd = passthrough_cmd(["-p", "--output-format", "json", "hello"])
        self.assertTrue(sup.handoff)
        self.assertTrue(sup.buffer_stdout)
        self.assertEqual(cmd[1:3], ["--settings", sup.overlay])
        self.assertIn("--session-id", cmd)


class B_NoContinuationRewrite(unittest.TestCase):
    def test_continue_with_separator_reaches_the_child_as_given(self):
        """`claude-seats -c -- "Fix bug"`: main() passes argv to the Supervisor unchanged (no `--resume <uuid>`)."""
        root = tempfile.mkdtemp(prefix="claude-seats-r2-b-")
        cwd = tempfile.mkdtemp(prefix="claude-seats-r2-cwd-")
        # a transcript the old build would have "resolved" `--continue` to
        prim = os.path.join(root, "seat-a")
        slug = "-" + os.path.realpath(cwd).strip("/").replace("/", "-").replace(".", "-")
        pd = os.path.join(prim, "projects", slug)
        os.makedirs(pd)
        open(os.path.join(pd, "00000000-0000-4000-8000-00000000abcd.jsonl"), "w").write("{}\n")
        json.dump({"primary": "a", "accounts": [{"id": "a", "label": "a", "configDir": prim}], "share": cs.DEFAULT_SHARE},
                  open(os.path.join(root, "accounts.json"), "w"))
        old_env = dict(os.environ)
        os.environ["CLAUDE_SEATS_ROOT"] = root
        mod = load_module()
        seen = {}

        class FakeSup:
            def __init__(self, r, argv, forced_seat=None):
                seen["argv"] = list(argv)

            def run(self):
                return 0
        mod.Supervisor = FakeSup
        mod._CLAUDE_BIN = "/opt/fake/claude"
        here = os.getcwd()
        os.chdir(cwd)
        try:
            with self.assertRaises(SystemExit):
                mod.main(["-c", "--", "Fix bug"])
        finally:
            os.chdir(here)
            os.environ.clear(); os.environ.update(old_env)
            shutil.rmtree(root); shutil.rmtree(cwd)
        self.assertEqual(seen["argv"], ["-c", "--", "Fix bug"])

    def test_continue_child_command_keeps_argv_order(self):
        sup, cmd = passthrough_cmd(["-c", "--", "Fix bug"])
        self.assertTrue(sup.handoff)
        self.assertEqual(cmd[1:], ["--settings", sup.overlay, "-c", "--", "Fix bug"])  # no --resume, `--` intact
        self.assertNotIn("--resume", cmd)
        self.assertFalse(hasattr(cs, "resolve_continue"))


class C_SessionIdentityFromTheRun(unittest.TestCase):
    def make(self, argv):
        sup = cs.Supervisor(fake_registry(TMP_ROOT), argv)
        sup.generation = 1
        sup.child = subprocess.Popen(["sleep", "5"])
        return sup

    def req(self, sup, session_id, gen="1", run_id=None):
        return {"token": "t", "reason": "threshold", "session_id": session_id, "hook_pid": os.getpid(),
                "run_id": run_id or sup.run_id, "gen": gen, "expires_at": time.time() + 5}

    def test_named_resume_accepts_the_hooks_uuid(self):
        sup = self.make(["--resume", "auth-refactor"])
        try:
            self.assertEqual(sup.sid, "auth-refactor")
            r = self.req(sup, "11111111-1111-4111-8111-111111111111")
            self.assertTrue(sup.valid_request(r))
            self.assertFalse(sup.valid_request(self.req(sup, "x", run_id="other-run")))
            self.assertFalse(sup.valid_request(self.req(sup, "x", gen="0")))
            # the hook's session id becomes the one to resume
            sup.sid = r["session_id"]
            args = cs.resume_args(sup.argv, sup.sid, False)
            self.assertEqual(args, ["--resume", "11111111-1111-4111-8111-111111111111"])
        finally:
            sup.child.kill()

    def test_fork_session_resumes_the_new_uuid(self):
        old = "22222222-2222-4222-8222-222222222222"
        new = "33333333-3333-4333-8333-333333333333"
        sup = self.make(["--resume", old, "--fork-session", "--model", "haiku"])
        try:
            self.assertTrue(sup.valid_request(self.req(sup, new)))
            sup.sid = new
            args = cs.resume_args(sup.argv, sup.sid, False)
            self.assertEqual(args, ["--model", "haiku", "--resume", new])
            self.assertNotIn("--fork-session", args)
            self.assertNotIn(old, args)
        finally:
            sup.child.kill()


class D_NoKillAfterHookRelease(unittest.TestCase):
    """A slow decision past the hook timeout leaves the child alive on every branch."""

    def run_branch(self, argv, pick_result, handoffs_before=0, last_handoff_recent=False):
        r = fake_registry(TMP_ROOT, seats=("a", "b", "c"))
        sup = cs.Supervisor(r, argv)
        sup.handoff = True
        sup.buffer_stdout = False
        sup.handoffs = handoffs_before
        a = r["accounts"][0]

        def fake_spawn(seat_, resume=False, continue_turn=False):
            sup.child = subprocess.Popen(["sleep", "30"])
            sup.generation += 1
            os.makedirs(os.path.dirname(sup.since), exist_ok=True)
            open(sup.since, "w").write("%f" % time.time())
            sup.killed_by_us = False
            req = {"ts": time.time(), "event": "StopFailure", "seat": "a", "session_id": "s1", "reason": "rate_limit",
                   "detail": "429", "continue_turn": True, "token": "tok", "hook_pid": os.getpid(),
                   "run_id": sup.run_id, "gen": str(sup.generation), "expires_at": time.time() + 0.6}
            cs.write_json(sup.events, req)

        def slow_pick(current=None, spent=(), fresh=False):
            time.sleep(1.0)              # the hook fails open meanwhile and withdraws its request
            try:
                os.remove(sup.events)
            except OSError:
                pass
            return pick_result

        sup.spawn = fake_spawn
        sup.pick = slow_pick
        sup.first_seat = lambda: (a, None)
        sup._prefetch_loop = lambda: None
        sup.restore_terminal = lambda: None
        saved = {k: getattr(cs, k, None) for k in ("login_state", "mark_native_limit", "mark")}
        cs.login_state = lambda *_a, **_k: "ok"
        cs.mark_native_limit = lambda *_a, **_k: []
        cs.mark = lambda *_a, **_k: None

        def user_quits():
            time.sleep(2.5)
            if sup.child and sup.child.poll() is None:
                sup.child.kill()
        threading.Thread(target=user_quits, daemon=True).start()
        old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = sup.run()
        finally:
            for s, h in old.items():
                signal.signal(s, h)
            for k, v in saved.items():
                if v is not None:
                    setattr(cs, k, v)
        return sup, rc

    def assert_survived(self, sup, rc, label, handoffs_before=0):
        self.assertFalse(sup.killed_by_us, label + ": supervisor signalled the child after the hook had released")
        self.assertEqual(rc, -9, label + ": child did not survive until the user ended it")
        self.assertEqual(sup.handoffs, handoffs_before, label)

    def test_handoff_branch_interactive(self):
        rows = [{"id": "a", "pct": 99, "bucket": "5-hour", "resets_at": None, "state": "spent"},
                {"id": "b", "pct": 10, "bucket": "5-hour", "resets_at": None, "state": "ok"}]
        self.assert_survived(*self.run_branch(["--model", "haiku"], ("b", rows)), "handoff/interactive")

    def test_handoff_branch_headless(self):
        rows = [{"id": "a", "pct": 99, "bucket": "5-hour", "resets_at": None, "state": "spent"},
                {"id": "b", "pct": 10, "bucket": "5-hour", "resets_at": None, "state": "ok"}]
        self.assert_survived(*self.run_branch(["-p", "--model", "haiku", "q"], ("b", rows)), "handoff/headless")

    def test_give_up_branch_headless_all_spent(self):
        rows = [{"id": "a", "pct": 99, "bucket": "5-hour", "resets_at": None, "state": "spent"},
                {"id": "b", "pct": None, "bucket": None, "resets_at": None, "state": "not logged in"}]
        self.assert_survived(*self.run_branch(["-p", "--model", "haiku", "q"], (None, rows)), "give-up/all spent")

    def test_give_up_branch_headless_too_many(self):
        rows = [{"id": "a", "pct": 99, "bucket": "5-hour", "resets_at": None, "state": "spent"},
                {"id": "b", "pct": 10, "bucket": "5-hour", "resets_at": None, "state": "ok"}]
        self.assert_survived(*self.run_branch(["-p", "--model", "haiku", "q"], ("b", rows), handoffs_before=2),
                             "give-up/too many", handoffs_before=2)

    def test_end_child_rechecks_right_before_the_signal(self):
        sup = cs.Supervisor(fake_registry(TMP_ROOT), [])
        sup.child = subprocess.Popen(["sleep", "30"])
        try:
            req = {"token": "t", "hook_pid": 2 ** 22 - 1, "expires_at": time.time() + 5}
            cs.write_json(sup.events, {})
            self.assertFalse(sup.end_child(req=req))        # hook gone -> no signal
            self.assertIsNone(sup.child.poll())
            self.assertFalse(sup.killed_by_us)
            req["hook_pid"] = os.getpid()
            self.assertTrue(sup.end_child(req=req, grace=2))  # hook alive -> SIGTERM
            self.assertIsNotNone(sup.child.poll())
        finally:
            if sup.child.poll() is None:
                sup.child.kill()
            try:
                os.remove(sup.events)
            except OSError:
                pass


class E_NativeLimitMarks(unittest.TestCase):
    def setUp(self):
        cs.save_state({"limits": {}})

    def test_fable_only_exhaustion_leaves_the_seat_selectable_for_sonnet(self):
        u = usage_ok("usage-scoped-spent.json", future=True)
        marked = cs.mark_native_limit("a", True, u, note="rate_limit")
        self.assertEqual(marked, ["scoped"])
        lim = cs.load_state()["limits"]["a"]
        self.assertNotIn("all", lim)
        scoped_reset = [l["resets_at"] for l in u["data"]["limits"] if l["kind"] == "weekly_scoped"][0]
        self.assertAlmostEqual(lim["scoped"]["until"], cs.parse_ts(scoped_reset), delta=1)
        self.assertIsNotNone(cs.limited("a", want_scoped=True))
        self.assertIsNone(cs.limited("a", want_scoped=False), "seat must stay selectable for --model sonnet")

    def test_session_bucket_exhaustion_blocks_every_model_until_its_own_reset(self):
        u = usage_ok("usage-session-spent.json", future=True)
        cs.mark_native_limit("a", False, u)
        lim = cs.load_state()["limits"]["a"]
        self.assertEqual(list(lim), ["session"])
        session_reset = [l["resets_at"] for l in u["data"]["limits"] if l["kind"] == "session"][0]
        self.assertAlmostEqual(lim["session"]["until"], cs.parse_ts(session_reset), delta=1)
        self.assertIsNotNone(cs.limited("a", want_scoped=False))

    def test_fallback_when_the_api_cannot_answer_is_model_scoped_and_one_hour(self):
        t0 = time.time()
        self.assertEqual(cs.mark_native_limit("a", True, {"ok": False, "reason": "timeout"}), ["scoped"])
        self.assertEqual(cs.mark_native_limit("b", False, None), ["all"])
        lim = cs.load_state()["limits"]
        self.assertAlmostEqual(lim["a"]["scoped"]["until"], t0 + 3600, delta=5)
        self.assertIsNone(cs.limited("a", want_scoped=False))
        self.assertIsNotNone(cs.limited("b", want_scoped=False))


if __name__ == "__main__":
    unittest.main()
