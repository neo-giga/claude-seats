# claude-seats

Run [Claude Code](https://code.claude.com) across several Claude subscriptions ("seats")
on one machine, and move a running conversation to the seat with the most usage left
when the current one runs dry. Official `claude` binary only, official logins only,
no proxy, no token juggling.

    claude-seats                      start on the seat with the most headroom, auto-handoff on
    claude-seats -p "..."             same, headless: reruns on the next seat if a usage limit ends the run
    claude-seats --account b ...      pin a seat (no handoff)
    claude-seats --status             seats, login state, recorded limits, what is still to do
    claude-seats --usage              live 5-hour / weekly / weekly-model numbers per seat
    claude-seats --add <id> <dir>     register a seat and print its login command
    claude-seats --sync               re-link shared config, re-copy mcpServers
    claude-seats --connectors         claude.ai connectors each seat still has to authorize

`claude` on its own is untouched and keeps running your default account exactly as before.

## Why not a proxy or token swapping

Several tools "switch accounts" by copying OAuth tokens between config directories, by
rewriting the Keychain entry under a running session, by refreshing tokens themselves, or
by putting a proxy in front of the API. This project does none of that, deliberately:

- Each seat is a normal Claude Code login in its own `CLAUDE_CONFIG_DIR`, created with
  `claude auth login` by you. claude-seats never runs login, logout or `setup-token`.
- Tokens are **read** (to call the official `/api/oauth/usage` endpoint with the seat's own
  token, the same call the `/usage` screen makes) and never copied, moved, rewritten or
  refreshed. An expired access token is refreshed by the official binary itself, when a
  turn runs on that seat.
- A "switch" is a new `claude` process on another seat, resuming the same session id.
  Nothing is injected into a running process.

**The caveat, plainly:** holding several paid subscriptions to extend your usage may conflict
with Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) and
[Usage Policy](https://www.anthropic.com/legal/aup). Read them and decide for yourself. This
tool keeps every account's own login and limits intact; it does not help you evade a limit
on one account, it moves you to another account that still has room.

## How it works

**Seats.** `accounts.json` (local, gitignored) lists seats: an id and a `CLAUDE_CONFIG_DIR`.
The default seat (`~/.claude`) runs with the variable *unset*: its config JSON is
`~/.claude.json`, and pointing `CLAUDE_CONFIG_DIR` at `~/.claude` makes Claude Code think it is
logged out. Every other seat has its own dir, its own Keychain item
(`Claude Code-credentials-<sha256(dir)[:8]>`) and its own login.

**Shared config.** Everything account-agnostic — `settings.json`, `CLAUDE.md`, `hooks/`,
`skills/`, `commands/`, `agents/`, `plugins/`, `projects/` (session history and memory) — is a
symlink from each seat into the primary seat, so every seat behaves identically and a session
started on one can be resumed on another. User-scope MCP servers live in
`<configDir>/.claude.json` (settings.json cannot hold them), so the `mcpServers` block is copied
from the primary to the others on every launch (`--sync` does it by hand).

Not shareable: claude.ai connectors (held server-side per account) and the OAuth grants of
HTTP MCP servers (per config dir). Each seat authorizes them once; `--connectors` lists what is
missing.

**Launch.** For each logged-in seat claude-seats reads `/api/oauth/usage` and takes the fullest
of its 5-hour, weekly and (when the session runs the model that has its own weekly cap)
weekly-model buckets. The seat with the lowest "tightest bucket" starts. One stderr line shows
the decision, e.g.

    claude-seats: seat a  (~/.claude) - a: 46% (5-hour, resets Tue 19:39); b: 71% (weekly, resets Fri 23:00)

**Handoff.** claude-seats stays alive as a supervisor; `claude` inherits the terminal (no PTY
relay). The session gets a `--settings` overlay with two hooks: `Stop` (every turn boundary)
and `StopFailure` with matcher `rate_limit`. If you passed your own `--settings`, it is
deep-merged with the overlay — Claude Code treats a repeated `--settings` flag as last-wins,
so this is the only way to add hooks without dropping yours. The shared `settings.json` is
never edited; plain `claude` never sees these hooks.

At a turn boundary the hook decides whether the seat is spent:

| signal | source | action |
|---|---|---|
| `StopFailure` with `error: rate_limit` | the API returned 429 | hand off, auto-continue the cut turn |
| "Usage limit reached" records in the transcript | Claude Code's own limit handling (it parks the session until reset) | hand off, auto-continue |
| tightest bucket ≥ threshold (default 95 %, `CLAUDE_SEATS_HANDOFF_AT`) | `/api/oauth/usage`, read at most once per 2 min per seat | hand off at this turn boundary (interactive only) |

The hook does not just report: it **waits** for the supervisor's answer (a few hundred
milliseconds, at most 8 s, then it fails open). While a Stop hook is running no new turn can
start, so the supervisor only ever ends `claude` at a turn boundary it is still holding; if the
hook has already returned, the supervisor leaves the session alone and retries at the next
boundary. "Stay" releases the hook and the session goes on. The supervisor then ends `claude`
with SIGTERM (it exits in under half a second and restores the terminal; after a forced kill the
supervisor restores the terminal itself), picks the seat with the most headroom from usage it
keeps pre-fetched in the background, and starts `claude --resume <same session id>` on it. If
the limit cut the turn, the resumed session is told to continue the task. One stderr line per
handoff:

    claude-seats: seat a at 96% (5-hour) -> b, resuming 7d2c…e1 

Headless (`-p`) rotation runs only for the simple shape: text or json output and a prompt on
the command line or stdin. It reruns only on a real limit, never on the threshold, at most
(seats − 1) times; stdout is buffered so you get exactly one run's output and exit code. In
these cases claude-seats is a **pure pass-through** instead — the plain `claude` child with
your argv untouched, output unbuffered, no hooks, no rerun — and says so in one stderr line:

| argv | why rotation is off |
|---|---|
| `-p --input-format stream-json` | a streaming protocol with queued turns; nothing may sit in the middle of it |
| `-p --output-format stream-json` | same |
| `--no-session-persistence` | nothing to resume |
| `--account <id>`, `CLAUDE_SEATS_HANDOFF=off` | you asked for a fixed seat / launch-time pick only |
| ambiguous arguments (below) | the prompt cannot be told apart from option values |

The launch-time seat pick still applies in pass-through mode. When **every** seat is spent
nothing loops and nothing is killed: the hook is released, the session stays on the current
seat, each seat's reset time is printed, and a headless run reports when `claude` itself exits.

Every handoff appends one JSON line to `runtime/handoffs.log` (time, session id, from, to,
reason, each seat's numbers).

A relaunch resumes the session id the hook reported — so `--resume <name>`, `--fork-session`
and `--continue` work: claude-seats never guesses a uuid up front. A relaunch has to know which
argument is your prompt. claude-seats parses options with the
arities from `claude --help` (a table in the code). When that is ambiguous — an option it does
not know, or a variadic option such as `--add-dir` with no prompt anywhere else — it turns
rotation off for that launch and says so on stderr rather than guess. Put the prompt first, or
after `--`, to keep rotation on. Limits are detected only from Claude Code's own structured
records and `StopFailure` payloads, never from prompt text that happens to mention a limit.
A `--settings` value that is not a JSON object with well-formed `hooks` stops the launch with
an error; your settings are merged verbatim, never weakened.

## Install

    git clone <this repo> ~/src/claude-seats
    cd ~/src/claude-seats && ./install.sh        # symlinks bin/claude-seats into ~/.local/bin
    claude-seats --status

`install.sh` prints the `PATH` line to add to your shell rc; it never edits the rc itself.
Pass `--copy` to copy instead of symlink, `--bin-dir` / `--root` to change locations. State
lives in the root (`CLAUDE_SEATS_ROOT`, default `~/.claude-accounts`): `accounts.json`,
`state.json`, `runtime/`.

Requirements: macOS (the Keychain is read with `security`), Claude Code, python3 (3.9+; the
system `/usr/bin/python3` is enough), bash. Linux is untested: `.credentials.json` is read
instead of the Keychain, nothing else is platform-specific.

## Adding a seat

    claude-seats --add a ~/.claude            # your existing default login (if not yet listed)
    claude-seats --add b ~/.claude-b          # creates that dir, links its shared config, prints:
    CLAUDE_CONFIG_DIR=~/.claude-b claude auth login

`--add` touches only the new seat; `--sync` is the command that re-links and re-syncs every seat.

Run that login yourself. Then `claude-seats --status` shows the seat as logged in, and
`--connectors` lists what to authorize on it (`claude-seats --account b`, then `/mcp`).

Do not use `claude setup-token` for a seat: it mints a `user:inference`-only token which
`/api/oauth/usage` rejects, so the seat's limits become unreadable and selection goes blind.
`tokenRef` (a 1Password CLI secret reference) in `accounts.json` is only a fallback for a host
without a Keychain.

## Turning it off

| want | do |
|---|---|
| launch-time pick only, no handoff | `CLAUDE_SEATS_HANDOFF=off claude-seats` (alias: `CC_HANDOFF=off`) |
| a fixed seat | `claude-seats --account b` |
| another threshold | `CLAUDE_SEATS_HANDOFF_AT=90` (alias: `CC_HANDOFF_AT`) |
| no automatic refresh turn on a stale seat | `CLAUDE_SEATS_REFRESH=off` (the probe is `claude -p "Reply ok" --model haiku --no-session-persistence`, run in a private temp dir) |
| a `claude` that is not the first on PATH | `CLAUDE_SEATS_CLAUDE_BIN=/path/to/claude` (the launcher refuses to run itself, and refuses to start inside its own session) |
| nothing at all | run `claude` |

Forget recorded limits with `claude-seats --clear [id]`.

## Tests

    /usr/bin/python3 -m unittest discover -s tests

Offline, from scrubbed real response shapes in `tests/fixtures/`. `tests/test_fix_round1.py`
and `tests/test_fix_round2.py` hold the regression tests from the review rounds. Real-account end-to-end checks live in
`tests/e2e/` behind `CLAUDE_SEATS_E2E=1` and need logged-in seats. `CLAUDE_SEATS_FORCE_LIMIT`,
and `CLAUDE_SEATS_FORCE_TARGET` exist for those tests only.

## Two traps worth remembering

- `CLAUDE_CONFIG_DIR=~/.claude` **logs you out** of the default seat (its JSON is
  `~/.claude.json`). The default seat runs with the variable unset.
- The `--settings` flag is last-wins. Add hooks through the merge claude-seats does, not by
  passing the flag twice.

MIT licensed.
