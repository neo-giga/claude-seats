#!/usr/bin/env python3
"""Real-account interactive handoff E2E, through a pty. Skipped unless CLAUDE_SEATS_E2E=1.

    CLAUDE_SEATS_E2E=1 CLAUDE_SEATS_ROOT=<root> python3 tests/e2e/interactive_handoff.py threshold
    CLAUDE_SEATS_E2E=1 CLAUDE_SEATS_ROOT=<root> python3 tests/e2e/interactive_handoff.py stopfailure

threshold    sets CLAUDE_SEATS_HANDOFF_AT below the start seat's real usage, so the first turn
             boundary hands off. (AC3)
stopfailure  lets the first turn end normally, then feeds the documented StopFailure(rate_limit)
             payload through the real hook command of the real session; the supervisor must hand
             off and auto-continue the cut turn. (AC4)

With only one logged-in seat, set CLAUDE_SEATS_FORCE_TARGET=<that seat> so the handoff
mechanics run seat -> same seat. The script prints every claude process's CLAUDE_CONFIG_DIR and
session id, the handoff line, the code-word answer after the resume, and the handoffs.log line.
Spends a few haiku turns.
"""
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

if os.environ.get("CLAUDE_SEATS_E2E") != "1":
    print("skipped: set CLAUDE_SEATS_E2E=1 to run the real-account E2E")
    sys.exit(0)

MODE = sys.argv[1] if len(sys.argv) > 1 else "threshold"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(REPO, "bin", "claude-seats")
ROOT = os.path.expanduser(os.environ.get("CLAUDE_SEATS_ROOT") or "~/.claude-accounts")
CODE = "MARIGOLD"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>78]|\r")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Pty:
    def __init__(self, argv, env):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        self.raw = b""

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                self.raw += data
        return True

    def text(self):
        return ANSI.sub("", self.raw.decode("utf-8", "replace"))

    def wait_for(self, pattern, timeout, start=0):
        rx = re.compile(pattern)
        end = time.time() + timeout
        while time.time() < end:
            m = rx.search(self.text()[start:])
            if m:
                return m
            if not self.pump(0.5):
                break
        return None

    def send(self, s):
        # the TUI drops a whole line written in one chunk; type it like a person
        for ch in s:
            os.write(self.fd, ch.encode())
            time.sleep(0.03)

    def alive(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False


def settle(p, quiet=2.0, limit=20.0):
    """Wait until the TUI has drawn nothing for `quiet` seconds (it is ready for keystrokes)."""
    end = time.time() + limit
    while time.time() < end:
        n = len(p.raw)
        p.pump(quiet)
        if len(p.raw) == n:
            return
    p.pump(1)


def claude_children(sup_pid):
    """[(pid, CLAUDE_CONFIG_DIR or '(unset)', seat, session-id)] for claude processes under the supervisor."""
    out = []
    try:
        kids = subprocess.run(["pgrep", "-P", str(sup_pid)], capture_output=True, text=True).stdout.split()
    except Exception:
        kids = []
    for k in kids:
        ps = subprocess.run(["ps", "-E", "-o", "command=", "-p", k], capture_output=True, text=True).stdout
        if "claude" not in ps:
            continue
        cfg = re.search(r"CLAUDE_CONFIG_DIR=(\S+)", ps)
        seat = re.search(r"CLAUDE_SEATS_SEAT=(\S+)", ps)
        sid = re.search(r"--(?:session-id|resume) ([0-9a-f-]{36})", ps)
        out.append((int(k), cfg.group(1) if cfg else "(unset)", seat.group(1) if seat else "?",
                    sid.group(1) if sid else "?"))
    return out


def main():
    # drop markers inherited from an enclosing Claude Code session (they turn transcript saving off)
    env = {k: v for k, v in os.environ.items() if not (k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE")}
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    env["CLAUDE_SEATS_ROOT"] = ROOT
    if MODE == "threshold":
        env.setdefault("CLAUDE_SEATS_HANDOFF_AT", "10")
    else:
        env.pop("CLAUDE_SEATS_HANDOFF_AT", None)
    env.pop("CLAUDE_SEATS_FORCE_LIMIT", None)
    os.chdir(REPO)
    log("mode=%s root=%s force_target=%s handoff_at=%s" % (
        MODE, ROOT, env.get("CLAUDE_SEATS_FORCE_TARGET"), env.get("CLAUDE_SEATS_HANDOFF_AT")))
    before = len(open(os.path.join(ROOT, "runtime", "handoffs.log")).read().splitlines()) \
        if os.path.exists(os.path.join(ROOT, "runtime", "handoffs.log")) else 0

    p = Pty([sys.executable, SCRIPT, "--model", "haiku"], env)
    m = p.wait_for(r"claude-seats: seat (\w+)", 30)
    assert m, "no launch line"
    first_seat = m.group(1)
    log("launch line: " + p.text()[m.start():m.end() + 160].splitlines()[0])
    # wait for the TUI to be ready for input; accept the workspace-trust dialog if it shows up
    # (that is claude's own first-run write for this folder, the same as a user pressing Enter)
    READY = r'Try"|forshortcuts|ctrl\+gtoedit|automodeon|bypasspermissions'
    m = p.wait_for(r"trust ?this ?folder|" + READY, 45)
    if m and "trust" in m.group(0):
        log("trust dialog: selecting 'Yes, I trust this folder'")
        p.send("\x1b[B")
        p.pump(0.5)
        p.send("\r")
        m = p.wait_for(READY, 45, start=m.end())
    assert m, "TUI never became ready\n" + p.text()[-1500:]
    settle(p)
    procs1 = claude_children(p.pid)
    log("claude process(es) before handoff: %s" % procs1)
    assert procs1, "no claude child found"
    sid1 = procs1[0][3]

    mark = len(p.text())
    first = "The code word is %s. Reply with only the code word spelled backwards, nothing else.\r" % CODE
    p.send(first)
    m = p.wait_for(CODE[::-1], 60, start=mark)
    if not m:  # keystrokes typed while the TUI was still initialising are dropped; type once more
        log("no reply yet; retyping the first prompt once")
        settle(p)
        p.send(first)
        m = p.wait_for(CODE[::-1], 90, start=mark)
    assert m, "no reply to the first prompt\n" + p.text()[-1500:]
    log("first turn answered")

    if MODE == "stopfailure":
        # let the Stop hook run (no handoff at the default threshold), then feed the documented
        # StopFailure(rate_limit) payload through the real hook command, exactly as claude would
        p.pump(6)
        assert len(claude_children(p.pid)) == 1 and claude_children(p.pid)[0][0] == procs1[0][0], "unexpected restart"
        since = [f for f in os.listdir(os.path.join(ROOT, "runtime", "events")) if f.endswith(".since")]
        assert len(since) == 1, since
        run_id = since[0][:-6]
        overlay = json.load(open(os.path.join(ROOT, "runtime", "overlay-%s.json" % run_id)))
        hook_cmd = overlay["hooks"]["StopFailure"][0]["hooks"][0]["command"]
        payload = json.load(open(os.path.join(REPO, "tests", "fixtures", "stopfailure-rate-limit.json")))
        payload["session_id"] = sid1
        payload["cwd"] = REPO
        payload["transcript_path"] = ""
        henv = dict(os.environ, CLAUDE_SEATS_RUN=run_id, CLAUDE_SEATS_SEAT=first_seat, CLAUDE_SEATS_ROOT=ROOT,
                    CLAUDE_SEATS_MODE="interactive", CLAUDE_SEATS_GEN="1")
        # (CLAUDE_SEATS_GEN is what the supervisor exports to its first child; the request must carry it)
        log("feeding StopFailure payload through the real hook command: %s" % hook_cmd)
        r = subprocess.run(hook_cmd, shell=True, input=json.dumps(payload), text=True, env=henv, capture_output=True)
        log("hook exit=%d stdout=%r stderr=%r" % (r.returncode, r.stdout[:200], r.stderr[:200]))

    m = p.wait_for(r"claude-seats: seat \w+ at [^\n]*-> \w+, resuming [0-9a-f-]{36}[^\n]*", 120, start=mark)
    assert m, "no handoff line\n" + p.text()[-2000:]
    handoff_line = m.group(0).strip()
    log("HANDOFF: " + handoff_line)
    sid_resumed = re.search(r"resuming ([0-9a-f-]{36})", handoff_line).group(1)
    p.wait_for(READY, 45, start=m.end())
    p.pump(5)
    procs2 = claude_children(p.pid)
    log("claude process(es) after handoff: %s" % procs2)
    assert procs2 and procs2[0][0] != procs1[0][0], "claude was not restarted"
    assert procs2[0][3] == sid1 == sid_resumed, "session id changed: %s %s %s" % (sid1, sid_resumed, procs2[0][3])

    if MODE == "stopfailure":
        m2 = p.wait_for(r"usage limit|continue|Continue|done|complete", 90, start=m.end())
        log("auto-continue visible: %s" % bool(m2))

    mark2 = len(p.text())
    p.send("What is the code word? Reply with only the word.\r")
    m = p.wait_for(CODE, 90, start=mark2)
    log("code word after resume: %s" % ("FOUND" if m else "NOT FOUND\n" + p.text()[-1500:]))
    p.pump(2)
    p.send("/exit\r")
    p.pump(3)
    if p.alive():
        p.send("\r")
        p.pump(3)
    if p.alive():
        os.kill(p.pid, signal.SIGTERM)
        p.pump(2)
    try:
        _, st = os.waitpid(p.pid, 0)
    except ChildProcessError:
        st = -1
    log("supervisor exited status=%s" % st)
    lines = open(os.path.join(ROOT, "runtime", "handoffs.log")).read().splitlines()
    new = lines[before:]
    log("handoffs.log new line(s): %d" % len(new))
    for l in new:
        print("   ", l[:600])
    result = {"mode": MODE, "session_id": sid1, "procs_before": procs1, "procs_after": procs2,
              "handoff_line": handoff_line, "code_word_found": bool(m), "handoff_log_lines": len(new)}
    print("RESULT " + json.dumps(result))
    return 0 if m and new else 1


if __name__ == "__main__":
    sys.exit(main())
