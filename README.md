# imessage-cli

**Visibility: public.**

Terminal access to your **own** iMessage / SMS history by reading the local
Messages database (`~/Library/Messages/chat.db`) read-only:

```bash
imsg chats                    # 1 row per conversation, most recent first
imsg read "alice"             # show a thread
imsg search "dinner"          # full-text search across all messages
imsg send "alice" "hi" --yes  # send a 1:1 message (dry-run without --yes)
```

Recent macOS removed message-text reading from the Messages scripting
dictionary (it only exposes `send`), so the SQLite store is the only way to read
history — the
CLI snapshots `chat.db` (plus its WAL/SHM sidecars) to a temp dir and queries
the copy, so it never locks or mutates the live database.

Contact names are resolved best-effort via the Contacts framework; without that
permission, raw phone/email handles are shown instead (commands still work).

## Prerequisites

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) — it runs the
script and resolves its dependencies on demand:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# or
brew install uv
```

The `npx skills` install path additionally requires Node.js (for `npx`).

### Full Disk Access (one-time)

`chat.db` is protected by macOS privacy (TCC), and Full Disk Access is the only
grant that covers it. Either:

- **Terminal**: System Settings → Privacy & Security → **Full Disk Access**,
  enable (or add with **+**) your terminal, e.g. **iTerm** or **Terminal**,
  then quit and relaunch it. `imsg` reads `chat.db` directly.
- **iMessage Reader helper** (Claude Desktop, or any app you would rather not
  trust with Full Disk Access): `imsg helper-install --yes`, then switch on
  **iMessage Reader** in the Full Disk Access pane it opens. See
  [iMessage Reader helper](#imessage-reader-helper).

`imsg helper-status` reports which path works and what to do next.

## Install

**A — Bundled launcher.** No install step. Clone the repo and invoke
`bin/imsg` (a symlink to `imessage_cli.py`) directly; the `#!/usr/bin/env -S uv run --script` shebang and
[PEP 723](https://peps.python.org/pep-0723/) inline metadata make `uv` pull
deps on the first run.

```bash
git clone <repo> imessage-cli
cd imessage-cli
./bin/imsg chats
```

**B — As an agent skill.** The repo ships a `SKILL.md` and the self-contained
`bin/imsg` launcher, in the
[Vercel Labs `skills`](https://github.com/vercel-labs/skills) format:

```bash
npx skills add ClementWalter/imessage-cli
```

This drops the skill under `~/.agents/skills/imessage-cli/` and symlinks it into
every supported agent runtime installed on your machine (Claude Code, Cursor,
Windsurf, Codex, Gemini CLI, …). Agents then drive the CLI by invoking the
bundled `bin/imsg` script directly.

To install locally for development instead, symlink the checkout so the skill
picks up live edits:

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)" ~/.claude/skills/imessage-cli
```

## Usage

```bash
imsg chats                       # 30 most-recent conversations
imsg chats --limit 200 --json    # everything, machine-readable
imsg read "alice"                # alice's thread; auto-resolves contact names
imsg read "alice" --match 2      # disambiguate when multiple chats match
imsg read "+15555550123"         # by raw handle
imsg read "Family" --limit 200   # a group chat
imsg search "dinner"             # substring search across all messages
imsg send "alice" "running late" # dry-run: prints resolved recipient + text
imsg send "alice" "running late" --yes        # actually sends (via Messages)
imsg send "alice" "hi" --sms                  # over SMS instead of iMessage
imsg send "alice" "hi" --handle +15555550123  # target an exact handle
imsg helper-status               # readable? directly or through the helper
imsg helper-install --yes        # install the iMessage Reader helper
```

`imsg --help` lists every subcommand; `imsg <cmd> --help` for per-command
options including `--json`, `--limit`, `--match`, `--handle`, `--sms`, `--yes`.

Every read command supports `--json` for structured output. Sending is an
outbound action: it **defaults to a dry-run** and only sends with `--yes`.

## How it works

- **Read-only by construction.** Reads happen against a temp-dir snapshot of
  `chat.db` (+ WAL/SHM), so the live Messages database is never locked or
  mutated. Only `send --yes` writes anything (an outgoing message, via the
  Messages app's AppleScript `send`).
- **Body-text decoding.** When a message's `text` column is empty (newer
  macOS), the body is decoded from the `attributedBody` blob; undecodable
  attachment-only messages show as `[attachment]`.
- **Attachments are resolved, not just flagged.** `read`/`search` copy every
  attachment out of the protected `~/Library/Messages/Attachments` store into
  `$TMPDIR/imsg-attachments/`; images (including HEIC, the iPhone default) are
  normalized to JPEG via `sips` so they're directly viewable. The message text
  gets a `[image: <path>]` / `[file: <path>]` tag, and `--json` output adds an
  `attachments: [{path, kind, name}]` array — an agent can then open the path
  directly instead of treating a photo as an opaque `[attachment]` placeholder
  (this is what a message actually says when the text alone is ambiguous or
  empty). Cached files persist for 24h (swept lazily on the next run) since a
  caller reads them *after* this process exits. A source file not on disk
  (offloaded to iCloud, never downloaded) is skipped rather than guessed at.
- **Tapbacks are folded into the message they decorate.** A reaction is its own
  row in `chat.db`, not a flag on the message it points at, so a naive read
  spells it out as a separate localized pseudo-message (`A ajouté un « J'aime »
  à « ... »`) that also eats a `--limit` slot. `read`/`search` instead replay
  adds and removals in date order — a tapback later taken back leaves nothing
  behind — and hang the survivors off their target as a `[👍 alice, 😂 me]` tag,
  with a `reactions: [{kind, sender}]` array in `--json`. A reaction whose
  target is older than the window keeps its own line, quoting the message it
  answers (`😂 → "En bon père de famille"`).
- **Timestamps** are converted from Apple absolute time to local time.

## iMessage Reader helper

Claude Desktop runs extensions as children of Claude.app, so macOS attributes
their file access to Claude: granting Claude Full Disk Access would grant it to
every extension and to the Claude Code / Cowork shells too. macOS has no
narrower permission for `~/Library/Messages`, so a dedicated helper app holds
it instead.

- **What it is.** `helper/ImessageReader.swift` (~150 lines, no dependencies),
  wrapped by `helper/build.py` into `iMessage Reader.app`
  (`com.github.clementwalter.imessage-reader`, background-only) and signed ad
  hoc. It reads `~/Library/Messages/chat.db` and writes only
  `~/Library/Application Support/imessage-cli/` (mode 700): `chat.db` (600),
  `status.json` and `helper.log`. No network, no other files.
- **What it does.** Every 5 s it checks the database's mtime; when it changed
  (and at most every 30 s) it copies it through the SQLite backup API into a
  temp file, switches the copy out of WAL mode and renames it into place, so a
  reader always sees a consistent database. `status.json` records the last
  check, the last copy and the last error.
- **How it runs.** `imsg helper-install --yes` copies the app to
  `~/Applications`, writes `~/Library/LaunchAgents/<bundle id>.plist`
  (RunAtLoad, KeepAlive) and loads it with `launchctl bootstrap gui/$UID`, then
  opens the Full Disk Access pane and reveals the app in Finder. The user
  switches it on; the helper retries every 5 s, so no restart is needed.
  `imsg helper-uninstall --yes` undoes all of it except the Full Disk Access
  entry, which only the user can remove.
- **How imsg uses it.** `chat.db` itself is read when possible (a terminal with
  Full Disk Access sees the live database); otherwise the helper's copy is
  read, with a warning when the helper has stopped reporting.
- **Building.** `uv run --script helper/build.py` (needs the Xcode command line
  tools) writes `helper/dist/iMessage Reader.app`, a universal arm64/x86_64
  build. `helper-install` uses that build, or builds it when it is missing and
  `swiftc` exists.

Limits: attachments are not copied, so without direct access photos and files
show as unavailable. The copy holds your whole message history in clear, as
readable as the original to any process running as you without sandboxing,
which is the same exposure as `chat.db` minus the Full Disk Access gate.

Ad hoc signing ties the Full Disk Access grant to the binary's hash: a rebuilt
helper may show up as a new app and need switching on again. With a Developer
ID certificate, `build.py` signs with `--sign "Developer ID Application: ..."
--options runtime --timestamp`, the app is notarized (`xcrun notarytool submit
--wait`) and stapled (`xcrun stapler staple`); the grant then follows the team
ID and bundle ID across rebuilds, and Gatekeeper accepts the app on any Mac.

## Dependencies

Declared inline via PEP 723 in `imessage_cli.py`, pinned exactly:

- `click` — CLI framework
- `pyobjc-framework-Contacts` — macOS-only, resolves handles to contact names

Everything else is Python stdlib (`sqlite3`, `shutil`, `subprocess`, …).

## Tests

```bash
uv run --script tests/test_imsg.py
```

The suite drives the CLI against an in-memory chat.db fixture, so it needs
neither Full Disk Access nor your real message history.

## Scope

Single-user personal tooling for **your own** account on **your own** Mac. It
reads a local, TCC-protected database you already have access to — there is no
network service, no account registration, and no way to read anyone else's
messages. macOS only.

## See also

Same idea — your own messages, from the terminal, for other platforms:

- [whatsapp-cli](https://github.com/ClementWalter/whatsapp-cli) — your personal
  WhatsApp chats (pairs as a linked device)
- [slack-user-cli](https://github.com/ClementWalter/slack-user-cli) — Slack via
  your existing browser session credentials
