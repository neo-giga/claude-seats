"""Offline tests for claude-seats. Run with either interpreter:

    /usr/bin/python3 -m unittest discover -s tests
    /opt/homebrew/bin/python3 -m unittest discover -s tests

Fixtures under tests/fixtures/ are real response shapes, scrubbed. Nothing here
touches the network, the Keychain, or any real config dir: CLAUDE_SEATS_ROOT is a
temp dir and the credential readers are stubbed.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPT = os.path.join(os.path.dirname(HERE), "bin", "claude-seats")

TMP_ROOT = tempfile.mkdtemp(prefix="claude-seats-test-root-")
os.environ["CLAUDE_SEATS_ROOT"] = TMP_ROOT
os.environ.pop("CLAUDE_SEATS_HANDOFF", None)
os.environ.pop("CLAUDE_SEATS_HANDOFF_AT", None)
os.environ.pop("CC_HANDOFF", None)
os.environ.pop("CC_HANDOFF_AT", None)


def load_module():
    loader = importlib.machinery.SourceFileLoader("claude_seats", SCRIPT)
    spec = importlib.util.spec_from_loader("claude_seats", loader)
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


class LimitMath(unittest.TestCase):
    def test_limit_report_reads_all_three_buckets(self):
        rep = cs.limit_report(usage_ok("usage-normal.json"))
        self.assertEqual(rep["session"]["pct"], 42)
        self.assertEqual(rep["overall"]["pct"], 26)
        self.assertEqual(rep["scoped"]["pct"], 23)
        self.assertEqual(rep["blocked"], [])

    def test_tightest_is_the_fullest_bucket_that_matters(self):
        rep = cs.limit_report(usage_ok("usage-scoped-spent.json"))
        self.assertEqual(cs.tightest(rep, want_scoped=True)[:2], (100, "weekly-fable"))
        self.assertEqual(cs.tightest(rep, want_scoped=False)[:2], (42, "5-hour"))

    def test_headroom_fraction(self):
        frac, reset = cs.headroom(usage_ok("usage-normal.json"))
        self.assertAlmostEqual(frac, 0.58)
        self.assertTrue(reset.startswith("2026-10-28T19:40"))
        self.assertEqual(cs.headroom({"ok": False, "reason": "x"}), (None, None))

    def test_blocked_lists_exhausted_buckets(self):
        rep = cs.limit_report(usage_ok("usage-session-spent.json"))
        self.assertEqual(rep["blocked"], ["session"])
        rep = cs.limit_report(usage_ok("usage-scoped-spent.json"))
        self.assertEqual(rep["blocked"], ["weekly_scoped(Fable)"])

    def test_want_scoped_for_model_hint(self):
        self.assertTrue(cs.want_scoped_for(None))
        self.assertTrue(cs.want_scoped_for("fable"))
        self.assertTrue(cs.want_scoped_for("claude-fable-5-1"))
        self.assertFalse(cs.want_scoped_for("haiku"))
        self.assertFalse(cs.want_scoped_for("sonnet"))


class Selection(unittest.TestCase):
    def test_picks_max_headroom_among_n_seats(self):
        spent = usage_ok("usage-session-spent.json")   # 100 % 5-hour
        normal = usage_ok("usage-normal.json")          # tightest 42 %
        better = json.loads(json.dumps(normal))
        for l in better["data"]["limits"]:
            l["percent"] = 10
        chosen, rows = cs.choose_seat([seat("a", usage=spent), seat("b", usage=normal), seat("c", usage=better),
                                       seat("d", usage=normal)])
        self.assertEqual(chosen, "c")
        self.assertEqual([r["id"] for r in rows], ["a", "b", "c", "d"])
        self.assertEqual(rows[0]["state"], "over threshold")

    def test_not_logged_in_seat_is_never_chosen(self):
        chosen, rows = cs.choose_seat([seat("a", usage=usage_ok("usage-session-spent.json")), seat("c", usable=False)])
        self.assertIsNone(chosen)
        self.assertEqual(rows[1]["state"], "not logged in")

    def test_all_spent_returns_none_with_reset_times(self):
        spent = usage_ok("usage-session-spent.json")
        chosen, rows = cs.choose_seat([seat("a", usage=spent), seat("b", usage=spent)], current="a")
        self.assertIsNone(chosen)
        self.assertTrue(all(r["resets_at"] for r in rows))
        text = cs.describe_rows(rows)
        self.assertIn("a: 100% (5-hour", text)
        self.assertIn("resets", text)

    def test_threshold_boundary(self):
        u = usage_ok("usage-normal.json")
        for l in u["data"]["limits"]:
            if l["kind"] == "session":
                l["percent"] = 95
        chosen, _ = cs.choose_seat([seat("a", usage=u)], threshold=95)
        self.assertIsNone(chosen)
        chosen, _ = cs.choose_seat([seat("a", usage=u)], threshold=96)
        self.assertEqual(chosen, "a")

    def test_current_seat_marked_spent_is_excluded(self):
        normal = usage_ok("usage-normal.json")
        chosen, rows = cs.choose_seat([seat("a", usage=normal), seat("b", usage=normal)], current="a", spent={"a"})
        self.assertEqual(chosen, "b")
        self.assertEqual(rows[0]["state"], "spent")
        chosen, _ = cs.choose_seat([seat("a", usage=normal, marked_until=time.time() + 60), seat("b", usage=normal)])
        self.assertEqual(chosen, "b")

    def test_unknown_usage_ranks_after_known_headroom_but_is_a_candidate(self):
        normal = usage_ok("usage-normal.json")  # 42 %
        chosen, _ = cs.choose_seat([seat("a", usage={"ok": False, "reason": "timeout"}), seat("b", usage=normal)])
        self.assertEqual(chosen, "b")
        worse = json.loads(json.dumps(normal))
        for l in worse["data"]["limits"]:
            l["percent"] = 80
        chosen, _ = cs.choose_seat([seat("a", usage={"ok": False, "reason": "timeout"}), seat("b", usage=worse)])
        self.assertEqual(chosen, "a")
        chosen, _ = cs.choose_seat([seat("a", usage=None), seat("b", usage=None)])
        self.assertEqual(chosen, "a")

    def test_ties_keep_the_current_seat(self):
        normal = usage_ok("usage-normal.json")
        chosen, _ = cs.choose_seat([seat("a", usage=normal), seat("b", usage=normal)], current="b")
        self.assertEqual(chosen, "b")

    def test_scoped_cap_only_matters_for_the_scoped_model(self):
        fable_spent = usage_ok("usage-scoped-spent.json")
        chosen, _ = cs.choose_seat([seat("a", usage=fable_spent)], want_scoped=True)
        self.assertIsNone(chosen)
        chosen, _ = cs.choose_seat([seat("a", usage=fable_spent)], want_scoped=False)
        self.assertEqual(chosen, "a")


class LoginState(unittest.TestCase):
    def test_states(self):
        now = 1_800_000_000.0
        a = {"id": "x", "dir": "/tmp/x"}
        ms = 1000
        self.assertEqual(cs.login_state(a, creds={}, now=now), "none")
        self.assertEqual(cs.login_state(a, creds={"accessToken": "t", "expiresAt": (now + 3600) * ms}, now=now), "ok")
        self.assertEqual(cs.login_state(a, creds={"accessToken": "t", "expiresAt": (now - 1) * ms,
                                                  "refreshToken": "r", "refreshTokenExpiresAt": (now + 1) * ms},
                                        now=now), "stale")
        self.assertEqual(cs.login_state(a, creds={"accessToken": "t", "expiresAt": (now - 1) * ms,
                                                  "refreshToken": "r", "refreshTokenExpiresAt": (now - 1) * ms},
                                        now=now), "expired")
        # the shape the binary leaves behind after a failed refresh: expiresAt 0
        self.assertEqual(cs.login_state(a, creds={"accessToken": "t", "expiresAt": 0, "refreshToken": "r",
                                                  "refreshTokenExpiresAt": (now - 1) * ms}, now=now), "expired")


class HookDecisions(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_stopfailure_rate_limit_requests_handoff_and_continue(self):
        req = cs.evaluate_turn(fixture("stopfailure-rate-limit.json"), "a", None, 95, True, now=self.now)
        self.assertEqual(req["reason"], "rate_limit")
        self.assertTrue(req["continue_turn"])
        self.assertEqual(req["session_id"], "00000000-0000-4000-8000-000000000001")

    def test_stopfailure_other_errors_do_nothing(self):
        p = dict(fixture("stopfailure-rate-limit.json"), error="server_error")
        self.assertIsNone(cs.evaluate_turn(p, "a", None, 95, True, now=self.now))

    def test_stop_over_threshold_requests_handoff_without_continue(self):
        u = usage_ok("usage-normal.json")
        for l in u["data"]["limits"]:
            if l["kind"] == "session":
                l["percent"] = 97
        req = cs.evaluate_turn(fixture("stop-payload.json"), "a", u, 95, True, now=self.now)
        self.assertEqual(req["reason"], "threshold")
        self.assertEqual((req["pct"], req["bucket"]), (97, "5-hour"))
        self.assertFalse(req["continue_turn"])

    def test_stop_under_threshold_does_nothing(self):
        req = cs.evaluate_turn(fixture("stop-payload.json"), "a", usage_ok("usage-normal.json"), 95, True, now=self.now)
        self.assertIsNone(req)
        req = cs.evaluate_turn(fixture("stop-payload.json"), "a", None, 95, True, now=self.now)
        self.assertIsNone(req)

    def test_stop_detects_native_limit_records_in_transcript(self):
        # the real records Claude Code wrote when a limit hit mid-session, re-stamped to "now"
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "s.jsonl")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.now - 30))
        with open(path, "w") as f:
            for line in open(os.path.join(FIX, "transcript-usage-limit.jsonl")):
                d = json.loads(line)
                d["timestamp"] = stamp
                f.write(json.dumps(d) + "\n")
        payload = dict(fixture("stop-payload.json"), transcript_path=path)
        req = cs.evaluate_turn(payload, "a", usage_ok("usage-normal.json"), 95, True, now=self.now)
        self.assertEqual(req["reason"], "usage_limit_reached")
        self.assertTrue(req["continue_turn"])
        self.assertIn("Usage limit reached", req["detail"])
        # older than the current run (an earlier handoff on the same session): ignored
        req = cs.evaluate_turn(payload, "a", usage_ok("usage-normal.json"), 95, True, now=self.now,
                               transcript_since=self.now)
        self.assertIsNone(req)
        shutil.rmtree(tmp)

    def test_forced_limit(self):
        req = cs.evaluate_turn(fixture("stop-payload.json"), "a", None, 95, True, now=self.now, forced=True)
        self.assertEqual(req["reason"], "forced")


class ArgHandling(unittest.TestCase):
    def test_parse_args(self):
        info = cs.parse_args(["-p", "--model", "haiku", "--settings", "/x.json", "--output-format", "json", "hello"])
        self.assertTrue(info["print"])
        self.assertEqual(info["model"], "haiku")
        self.assertEqual((info["settings"], info["settings_idx"]), ("/x.json", 3))
        self.assertEqual(info["prompt_idx"], 7)
        self.assertFalse(info["stream"])
        info = cs.parse_args(["--resume", "abc", "-p", "--output-format", "stream-json", "--verbose", "q"])
        self.assertEqual(info["resume"], "abc")
        self.assertTrue(info["stream"])
        self.assertEqual(info["prompt_idx"], 6)
        self.assertEqual(cs.parse_args(["--resume"])["resume"], "")
        self.assertIsNone(cs.parse_args(cs.normalize_argv(["--model=sonnet"]))["prompt_idx"])

    def test_resume_args_drop_prompt_and_session_id(self):
        out = cs.resume_args(["--session-id", "old", "-p", "--model", "haiku", "What is 2+2?"], "sid-1", False)
        self.assertEqual(out, ["-p", "--model", "haiku", "--resume", "sid-1"])
        out = cs.resume_args(["-p", "--resume", "old", "ask"], "sid-1", True)
        self.assertEqual(out, ["-p", "--resume", "sid-1", cs.CONTINUE_PROMPT])
        out = cs.resume_args(["--continue", "--model", "sonnet"], "sid-2", False)
        self.assertEqual(out, ["--model", "sonnet", "--resume", "sid-2"])

    def test_merged_settings_keeps_caller_hooks_and_adds_ours(self):
        caller = {"permissions": {"allow": ["Bash(ls:*)"]},
                  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}
        m = cs.merged_settings(caller, "python3 /me --hook")
        self.assertEqual(m["permissions"], caller["permissions"])
        self.assertEqual(len(m["hooks"]["PreToolUse"]), 1)
        self.assertEqual([g["hooks"][0]["command"] for g in m["hooks"]["Stop"]], ["theirs", "python3 /me --hook"])
        self.assertEqual(m["hooks"]["StopFailure"][0]["matcher"], "rate_limit")
        self.assertEqual(caller["hooks"]["Stop"][0]["hooks"][0]["command"], "theirs")  # caller untouched
        m = cs.merged_settings(None, "h")
        self.assertEqual(set(m["hooks"]), {"Stop", "StopFailure"})

    def test_load_settings_arg_accepts_file_or_json(self):
        self.assertEqual(cs.load_settings_arg('{"a": 1}'), {"a": 1})
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "s.json")
        json.dump({"b": 2}, open(p, "w"))
        self.assertEqual(cs.load_settings_arg(p), {"b": 2})
        self.assertEqual(cs.load_settings_arg(None), {})
        shutil.rmtree(tmp)

    def test_env_switches(self):
        os.environ["CLAUDE_SEATS_HANDOFF"] = "off"
        self.assertFalse(cs.handoff_enabled())
        del os.environ["CLAUDE_SEATS_HANDOFF"]
        os.environ["CC_HANDOFF"] = "off"
        self.assertFalse(cs.handoff_enabled())
        del os.environ["CC_HANDOFF"]
        self.assertTrue(cs.handoff_enabled())
        os.environ["CLAUDE_SEATS_HANDOFF_AT"] = "80"
        self.assertEqual(cs.handoff_threshold(), 80.0)
        del os.environ["CLAUDE_SEATS_HANDOFF_AT"]
        self.assertEqual(cs.handoff_threshold(), 95.0)


class Registry(unittest.TestCase):
    def test_add_creates_registry_dir_and_login_line(self):
        root = tempfile.mkdtemp(prefix="claude-seats-reg-")
        home = tempfile.mkdtemp(prefix="claude-seats-home-")
        old = dict(os.environ)
        os.environ["CLAUDE_SEATS_ROOT"] = root
        os.environ["HOME"] = home
        mod = load_module()
        import io
        import contextlib
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mod.cmd_add(None, ["x", os.path.join(home, "seat-x")])
                mod.cmd_add(mod.reg(), ["y", os.path.join(home, "seat-y")])
            r = mod.reg()
            self.assertEqual([a["id"] for a in r["accounts"]], ["x", "y"])
            self.assertEqual(r["primary"], "x")
            self.assertTrue(os.path.isdir(os.path.join(home, "seat-y")))
            self.assertIn("CLAUDE_CONFIG_DIR=%s claude auth login" % os.path.join(home, "seat-y"), buf.getvalue())
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", buf.getvalue())
            # neither seat is logged in: not usable, status says so, nothing is picked
            self.assertFalse(any(mod.usable(a) for a in r["accounts"]))
            chosen, rows = mod.choose_seat(mod.seat_infos(r, probe=False))
            self.assertIsNone(chosen)
            self.assertEqual([x["state"] for x in rows], ["not logged in", "not logged in"])
        finally:
            os.environ.clear()
            os.environ.update(old)
            shutil.rmtree(root)
            shutil.rmtree(home)


if __name__ == "__main__":
    unittest.main()
