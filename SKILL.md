---
name: imessage-cli
description:
  "Read your own iMessage / SMS history from the terminal via the bundled
  `imsg` command. Reads the local Messages SQLite database
  (`~/Library/Messages/chat.db`) directly, since AppleScript can no longer read
  message text. `imsg chats` lists conversations, `imsg read <name>` shows a
  thread, `imsg search <text>` searches across messages, `imsg send <name>
  <text>` sends one (dry-run unless `--yes`). Resolves contact names via the
  Contacts framework. Requires Full Disk Access for the terminal app
  (iTerm/Terminal). macOS only. Use when the user wants to read, search, or send
  their personal iMessages/SMS."
---

# iMessage reader CLI

Terminal access to your **own** iMessage / SMS history by reading the local
Messages database (`~/Library/Messages/chat.db`) read-only.

Why a database reader and not AppleScript: recent macOS removed message-text
reading from the Messages scripting dictionary (it only exposes `send`), so the
SQLite store is the only way to read history.

## How to invoke

Invoke it as **`imsg`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/imsg`, so it always runs the current checkout: a `git pull`, or even
an uncommitted edit, takes effect immediately with nothing to reinstall.

```bash
imsg chats
```

Examples in this doc are written that way. If `imsg` is not on `$PATH`, run the
bundled launcher `bin/imsg` resolved against this skill's own directory (PEP 723
— `uv` resolves deps inline on first run), or link it once:

```bash
ln -sfn <skill-dir>/bin/imsg ~/.local/bin/imsg
```

## Prerequisite: Full Disk Access (one-time)

`chat.db` is protected by macOS privacy (TCC). Grant the **terminal app that
runs the command** Full Disk Access:

1. System Settings → Privacy & Security → **Full Disk Access**
2. Enable (or add with **+**) your terminal — e.g. **iTerm** or **Terminal**
3. **Quit and relaunch** that terminal app (required for it to take effect)

Until then every command fails with `authorization denied`. Contact-name
resolution additionally uses Contacts access; without it, raw phone/email
handles are shown instead (commands still work).

## When to use

Trigger when the user wants to **read, search, or send their personal
iMessages/SMS** — "show my texts with X", "what did Y message me", "search my
messages for Z", "text X that …".

Do **not** use it to read or send on someone else's account. Sending is an
outbound action: confirm the exact recipient and text with the user before
running `send --yes`.

## Commands

### `imsg chats [--limit N] [--json]`

List conversations, most recent first. Group chats show their name; 1:1 chats
resolve to a contact name when possible, else the handle.

### `imsg read <query> [--limit N] [--match N] [--json]`

Show messages from the chat best matching `<query>` (fuzzy against contact name,
handle, or group name). If several chats match, it prints a numbered list — pick
one with `--match N`.

```bash
imsg read jason            # most likely "Jason" thread
imsg read jason --match 2  # 2nd match when ambiguous
imsg read "+33612345678"   # by handle
imsg read "Family" --limit 200
```

### `imsg search <text> [--limit N] [--json]`

Full-text substring search across all messages.

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
- **Timestamps** are converted from Apple absolute time to local time.
- Every read command supports `--json` for structured output.
