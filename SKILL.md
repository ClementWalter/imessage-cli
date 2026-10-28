---
name: imessage-cli
description:
  "Read your own iMessage / SMS history from the terminal via the bundled
  `imsg` command. Reads the local Messages SQLite database
  (`~/Library/Messages/chat.db`) directly, since AppleScript can no longer read
  message text. `imsg chats` lists conversations, `imsg read <name>` shows a
  thread, `imsg search <text>` searches across messages, `imsg send <name>
  <text>` sends one (dry-run unless `--yes`). Resolves contact names via the
  Contacts framework. Needs Full Disk Access for the terminal, or the iMessage
  Reader helper (`imsg helper-install`) when run from Claude Desktop. macOS only. Use when the user wants to read, search, or send
  their personal iMessages/SMS."
---

# iMessage reader CLI

Terminal access to your **own** iMessage / SMS history by reading the local
Messages database (`~/Library/Messages/chat.db`) read-only.

Why a database reader and not AppleScript: recent macOS removed message-text
reading from the Messages scripting dictionary (it only exposes `send`), so the
SQLite store is the only way to read history.

## How to invoke

Invoke it as **`imsg`** — on `$PATH` via `claudine/scripts/link_clis.py`, which
links this repo's `bin/imsg` into `~/.local/bin`, so it always runs the current
checkout: a `git pull`, or even an uncommitted edit, takes effect immediately
with nothing to reinstall.

```bash
imsg chats
```

If `imsg` is not on `$PATH`, run `<skill-dir>/bin/imsg` (a symlink to
`imessage_cli.py`, a PEP 723 script — `uv` resolves deps inline on first run).

## Authentication

No login: the CLI reads the local Messages database. It needs macOS privacy
permissions instead.

### Full Disk Access (one-time)

`chat.db` is protected by macOS privacy (TCC), and the only grant that covers it
is Full Disk Access. Two ways to provide it:

- **Terminal**: give your terminal (iTerm, Terminal) Full Disk Access in System
  Settings → Privacy & Security → **Full Disk Access**, then quit and relaunch
  it. `imsg` then reads `chat.db` directly.
- **Claude Desktop / Cowork** (the iMessage extension): do **not** grant Claude
  Full Disk Access, it would extend to every extension and shell Claude runs.
  Instead run `imsg helper-install --yes`. It installs **iMessage Reader**
  (`~/Applications/iMessage Reader.app`, a background LaunchAgent), opens the
  Full Disk Access pane and shows the app in Finder; the user switches on
  "iMessage Reader" (or drags it into the list). The helper then keeps a copy
  of `chat.db` in `~/Library/Application Support/imessage-cli/` (owner-only),
  checked every 5 s and recopied at most every 30 s when it changed, and `imsg`
  reads that copy whenever it cannot read `chat.db` itself. No restart needed.

`imsg helper-status` tells which path is active and the one next step if
neither works. Contact-name resolution additionally uses Contacts access;
without it, raw phone/email handles are shown instead (commands still work).

## Output conventions

- Default: compact text, one line per message (`ts  sender: text`) or per chat.
- `--json` on every read command: structured rows, with `attachments` and
  `reactions: [{kind, sender}]` arrays.
- stdout carries data only; errors go to stderr with a non-zero exit.
- **Timestamps** are converted from Apple absolute time to local time.
- Attachment-only messages whose body can't be decoded show as `[attachment]`;
  resolved photos/files show as `[image: <path>]` / `[file: <path>]`.
- Tapbacks are folded onto their message as a `[👍 alice, 😂 me]` tag.

## Commands

```bash
# Read
imsg chats [--limit N] [--json]                        # conversations, most recent first
imsg read <query> [--limit N] [--match N] [--json]     # one thread (contact name, handle or group name)
imsg search <text> [--limit N] [--json]                # substring search across all messages
imsg helper-status [--json]                            # readable? direct or via the helper, next step
# Write (dry-run unless --yes)
imsg send <query> <text> [--match N] [--handle H] [--sms] [--yes]
imsg helper-install [--yes]                            # install iMessage Reader, open Full Disk Access pane
imsg helper-uninstall [--yes]                          # stop and remove it and its copy
```

### `imsg chats [--limit N] [--json]`

List conversations, most recent first. Group chats show their name; 1:1 chats
resolve to a contact name when possible, else the handle.

### `imsg read <query> [--limit N] [--match N] [--json]`

Show messages from the chat best matching `<query>` (fuzzy against contact name,
handle, or group name). If several chats match, it prints a numbered list (most
recent first) and exits 1 — pick one with `--match N`.

```bash
imsg read jason --match 1  # most recent chat matching "jason"
imsg read jason --match 2  # 2nd most recent
imsg read "+33612345678"   # by handle
imsg read "Family" --limit 200
```

### `imsg search <text> [--limit N] [--json]`

Full-text substring search across all messages.

### `imsg helper-status [--json]`

Whether messages are readable: `ready`, `direct_access`, the helper app and
LaunchAgent, how long ago the helper last checked and wrote its copy, its last
error (`no Full Disk Access: ...` until the user switches it on), and
`next_step`, the one action that unblocks reading.

### `imsg helper-install [--yes]` / `imsg helper-uninstall [--yes]`

Set up (or remove) the iMessage Reader helper described under Full Disk Access.
Dry run without `--yes`. After `helper-install --yes`, tell the user to switch
on "iMessage Reader" in the Full Disk Access window that opened, then poll
`helper-status` until `ready` is true. Reinstalling the same build keeps the
grant; a rebuilt helper may need switching on again (ad hoc signature).

### `imsg send <query> <text> [--match N] [--handle H] [--sms] [--yes]`

Send a message to the 1:1 conversation matching `<query>`. **Defaults to a
dry-run** that prints the resolved recipient and text without sending; pass
`--yes` to actually send (via the Messages app's AppleScript `send`). Ambiguous
matches print a numbered list — pick with `--match N`, or target a raw handle
with `--handle`. `--sms` sends over SMS instead of iMessage.

```bash
imsg send jason "running late"          # dry-run: shows recipient + text
imsg send jason "running late" --yes    # actually sends
```

## Recipes

```bash
# The 1:1 thread with someone (most recent matching chat)
imsg read "Jane Doe" --match 1 --limit 100

# What did a group say recently, as JSON for filtering
imsg read Family --limit 200 --json

# Find the message that mentioned an address or a code
imsg search "digicode" --limit 20

# Text someone: dry-run, check the recipient, then send
imsg send "Jane Doe" "running late" --match 1
imsg send "Jane Doe" "running late" --match 1 --yes
```

## Gotchas

- **A person's name usually matches several chats** (their 1:1 thread plus every
  group they're in), and `read`/`send` then print a numbered list and exit 1
  instead of picking one. The list is most recent first, so for "the thread with
  X" pass `--match 1` from the start: `imsg read "Jane Doe" --match 1`. For
  `send`, read the list and pick the recipient deliberately.
- Every read fails with "chat.db is not readable" until either the terminal has
  Full Disk Access (and was relaunched) or the iMessage Reader helper is
  installed and switched on: run `imsg helper-status` for the next step.
- Through the helper, the newest messages can lag up to ~35 s behind.
- Attachments live in `~/Library/Messages/Attachments`, which the helper does
  not copy: without direct access, photos and files show as
  `[attachment: unavailable]`.
- Without Contacts access, raw phone/email handles are shown instead of names
  (commands still work).
- A photo never downloaded from iCloud is skipped, not guessed at; open the
  `[image: <path>]` file with a Read tool rather than assuming its content.
- Sending is outbound: confirm the exact recipient and text with the user before
  `send --yes`.

## When to use

Trigger when the user wants to **read, search, or send their personal
iMessages/SMS** — "show my texts with X", "what did Y message me", "search my
messages for Z", "text X that …".

Do **not** use it to read or send on someone else's account. Sending is an
outbound action: confirm the exact recipient and text with the user before
running `send --yes`.

## Notes

- **Reading is read-only by construction.** The CLI snapshots `chat.db` (+ its
  WAL/SHM sidecars) to a temp dir and queries the copy, so it never locks or
  mutates the live Messages database. Only `send --yes` writes anything (an
  outgoing message).
- **Body text decoding.** When a message's `text` column is empty (newer macOS),
  the body is decoded from the `attributedBody` blob; undecodable
  attachment-only messages show as `[attachment]`.
- **Photos and files are resolved, not just flagged.** `read`/`search` copy
  every attachment out into `$TMPDIR/imsg-attachments/`; images (incl. HEIC)
  are normalized to JPEG via `sips`. The text shows a `[image: <path>]` /
  `[file: <path>]` tag and `--json` adds an `attachments` array — open the
  path with a Read tool to actually see the photo instead of assuming the
  conversation's topic didn't change at that message. A source never
  downloaded from iCloud is skipped, not guessed at.
- **Tapbacks (reactions) are attached to their message.** They are separate rows
  in `chat.db`, so `read`/`search` fold them back onto the message they point
  at as a `[👍 alice, 😂 me]` tag (`reactions: [{kind, sender}]` in `--json`),
  after replaying adds and removals so a tapback someone took back is gone.
  They no longer count against `--limit`. A reaction whose target is older than
  the window shown keeps its own line, quoting that target
  (`😂 → "En bon père de famille"`).

## Tests

```bash
uv run --script tests/test_imsg.py
```

## Project layout

```
imessage-cli/
  SKILL.md            # this file
  README.md
  imessage_cli.py     # the CLI (PEP 723 script)
  bin/imsg            # symlink -> ../imessage_cli.py — invoke this
  helper/             # iMessage Reader: Swift source, Info.plist, build.py
  tests/test_imsg.py  # offline tests against an in-memory chat.db
```
