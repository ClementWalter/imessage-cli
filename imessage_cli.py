#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "pyobjc-framework-Contacts==12.1; sys_platform == 'darwin'",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Read your own iMessage / SMS history from the local Messages database.

macOS Messages stores everything in a SQLite database at
`~/Library/Messages/chat.db`; AppleScript can no longer read message text, so
this CLI goes straight to that database (read-only) instead. It is the iMessage
counterpart to the WhatsApp/Slack personal CLIs: list conversations, read a
thread, search across messages.

Two macOS specifics drive the design:

* **Full Disk Access.** `chat.db` is TCC-protected. A terminal with Full Disk
  Access reads it directly. Anything else (Claude Desktop, which would extend
  the grant to every extension it runs) reads the copy kept by the iMessage
  Reader helper app (`helper/`), which alone holds Full Disk Access:
  `imsg helper-install` sets it up, `imsg helper-status` checks it.
* **WAL + text encoding.** The DB uses write-ahead logging while Messages is
  live, and recent macOS stores body text in the `attributedBody` blob rather
  than the `text` column. We snapshot the DB files to a temp dir before reading,
  and decode `attributedBody` when `text` is empty.

Contact names are resolved best-effort via the Contacts framework; if that
permission is absent the raw handle (phone/email) is shown instead.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr
)
log = logging.getLogger("imsg")

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
# Apple absolute time (seconds since 2001-01-01) → Unix epoch offset.
APPLE_EPOCH = 978307200
# The iMessage Reader helper (helper/ImessageReader.swift) keeps a consistent
# copy of chat.db here; these names must match the Swift source and Info.plist.
HELPER_DIR = Path.home() / "Library" / "Application Support" / "imessage-cli"
HELPER_COPY = HELPER_DIR / "chat.db"
HELPER_STATUS = HELPER_DIR / "status.json"
HELPER_LOG = HELPER_DIR / "helper.log"
HELPER_BUNDLE_ID = "com.github.clementwalter.imessage-reader"
HELPER_APP_NAME = "iMessage Reader.app"
HELPER_EXECUTABLE = "imessage-reader"
HELPER_SOURCE_DIR = Path(__file__).resolve().parent / "helper"
# Prebuilt app shipped next to the source (the extension bundle).
HELPER_BUILT_APP = HELPER_SOURCE_DIR / "dist" / HELPER_APP_NAME
HELPER_INSTALLED_APP = Path.home() / "Applications" / HELPER_APP_NAME
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{HELPER_BUNDLE_ID}.plist"
FDA_PANE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
# The helper checks chat.db every 5 s and recopies it (at most every 30 s) when
# it changed, so a report older than this means it is stopped or stuck.
HELPER_STALE_SECONDS = 120
# Snapshot temp dirs share this prefix so they can be recognised and swept.
_SNAPSHOT_PREFIX = "imsg-"
# A snapshot older than this is certainly abandoned (a run finishes in
# seconds), so sweeping it can never race a live concurrent invocation.
_SNAPSHOT_STALE_SECONDS = 3600

# Attachments are copied out here so a caller can Read() them after this
# process exits — unlike the chat.db snapshot, these must NOT be deleted at
# exit. Swept lazily instead (see _sweep_stale_attachment_cache).
ATTACHMENT_CACHE_DIR = Path(tempfile.gettempdir()) / "imsg-attachments"
_ATTACHMENT_STALE_SECONDS = 24 * 3600
# HEIC/HEIF (the iPhone default) is not a format most image readers accept;
# normalize every image-like attachment to JPEG via `sips` so it is always
# directly viewable, not just for HEIC.
_IMAGE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".tiff", ".tif", ".bmp", ".webp"}

# Tapbacks (reactions) are not a column on the message they decorate: each one
# is its own `message` row whose `associated_message_guid` points at the target.
# Type 2000+n adds reaction n, 3000+n removes that same reaction again.
_REACTION_ADD_BASE = 2000
_REACTION_REMOVE_BASE = 3000
_REACTION_MAX = 3999
_REACTION_KINDS = {0: "❤️", 1: "👍", 2: "👎", 3: "😂", 4: "‼️", 5: "❓"}
# Kind 6 is a free-form emoji tapback; the emoji itself sits in its own column.
_REACTION_EMOJI_KIND = 6
# How much of a reacted-to message to quote when that message is outside the
# window being printed.
_TARGET_SNIPPET_CHARS = 60


def _sweep_stale_snapshots() -> None:
    """Delete snapshot dirs left by earlier runs that exited before cleanup.

    chat.db is large (hundreds of MB); without this, every crashed or killed
    invocation would leak a full copy forever. Bounded by mtime age so a
    snapshot a concurrent run is still reading is never removed. Best-effort:
    permission/races on individual dirs are ignored.
    """
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - _SNAPSHOT_STALE_SECONDS
    for entry in root.glob(f"{_SNAPSHOT_PREFIX}*"):
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError as exc:  # noqa: PERF203 — one bad entry mustn't abort the sweep
            log.debug("could not sweep %s: %s", entry, exc)


def _helper_report() -> dict:
    """The helper's last status.json, or {} when it has never written one."""
    try:
        return json.loads(HELPER_STATUS.read_text())
    except (OSError, ValueError) as exc:
        log.debug("no helper status at %s: %s", HELPER_STATUS, exc)
        return {}


def _helper_current(report: dict) -> bool:
    """Whether the helper recently confirmed its copy matches chat.db.

    The copy's own mtime says nothing when Messages is idle (an unchanged
    database is not recopied), so freshness comes from the helper's last check.
    """
    checked = report.get("checked_at") or 0
    return bool(report.get("ok")) and time.time() - checked <= HELPER_STALE_SECONDS


def _db_source() -> Path:
    """The database to read: chat.db itself when readable, else the helper's copy.

    Direct access wins so a terminal with Full Disk Access always sees the live
    database; the helper's copy serves every process without that grant.
    """
    if _readable(CHAT_DB):
        return CHAT_DB
    if _readable(HELPER_COPY):
        if not _helper_current(_helper_report()):
            log.warning(
                "reading the iMessage Reader copy, which may be out of date: "
                "the helper is not reporting, run `imsg helper-status`"
            )
        return HELPER_COPY
    if not CHAT_DB.exists():
        raise click.ClickException(f"{CHAT_DB} not found — is this macOS Messages?")
    raise click.ClickException(
        "chat.db is not readable and the iMessage Reader helper has no copy yet. "
        "Run `imsg helper-install --yes`, then in System Settings → Privacy & "
        "Security → Full Disk Access switch on \"iMessage Reader\" (drag it in "
        "from ~/Applications if it is not listed). `imsg helper-status` shows "
        "progress. A terminal that has Full Disk Access reads chat.db directly."
    )


def _snapshot_db() -> Path:
    """Copy the database (+ WAL/SHM sidecars) to a temp dir and return the copy.

    Reading the live DB risks lock contention with Messages and a half-applied
    WAL; a private copy sidesteps both and lets SQLite checkpoint freely. The
    copy is registered for deletion at process exit so it does not accumulate.
    """
    source = _db_source()
    _sweep_stale_snapshots()
    tmp = Path(tempfile.mkdtemp(prefix=_SNAPSHOT_PREFIX))
    # Remove this run's copy on exit — covers normal exit, ClickException, and
    # SystemExit alike, so a ~195 MB chat.db copy is never left behind.
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    for suffix in ["", "-wal", "-shm"]:
        src = source.with_name(source.name + suffix)
        if src.exists():
            shutil.copy2(src, tmp / CHAT_DB.with_name(CHAT_DB.name + suffix).name)
    return tmp / CHAT_DB.name


def _readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_snapshot_db()))
    conn.row_factory = sqlite3.Row
    return conn


def _sweep_stale_attachment_cache() -> None:
    """Delete cached attachment copies older than a day.

    Unlike the chat.db snapshot these outlive the process on purpose (a
    caller reads them after this command exits), so they're swept by age at
    the start of the next run instead of at exit.
    """
    if not ATTACHMENT_CACHE_DIR.exists():
        return
    cutoff = time.time() - _ATTACHMENT_STALE_SECONDS
    for entry in ATTACHMENT_CACHE_DIR.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError as exc:  # noqa: PERF203 — one bad entry mustn't abort the sweep
            log.debug("could not sweep attachment cache %s: %s", entry, exc)


def _fetch_attachments(conn: sqlite3.Connection, mids: list[int]) -> dict[int, list[dict]]:
    """Batch-fetch attachment rows for a set of message ROWIDs."""
    if not mids:
        return {}
    placeholders = ",".join("?" for _ in mids)
    rows = conn.execute(
        f"""
        SELECT maj.message_id AS mid, a.filename AS path, a.mime_type AS mime,
               a.transfer_name AS name
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
        """,
        mids,
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["mid"], []).append(
            {"path": r["path"], "mime": r["mime"], "name": r["name"]}
        )
    return out


def _resolve_attachment(entry: dict) -> dict | None:
    """Copy (and, for images, normalize to JPEG) one attachment into the
    readable cache dir. Returns None if the source file isn't on disk (e.g.
    offloaded to iCloud and never downloaded) — an honest gap, not a guess.
    """
    raw = entry.get("path")
    if not raw:
        return None
    src = Path(raw).expanduser()
    if not src.exists():
        return None
    ATTACHMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(src).encode()).hexdigest()[:20]
    if src.suffix.lower() in _IMAGE_EXTS:
        dst = ATTACHMENT_CACHE_DIR / f"{key}.jpg"
        if not dst.exists():
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0 or not dst.exists():
                log.debug("sips conversion failed for %s: %s", src, result.stderr)
                return None
        kind = "image"
    else:
        dst = ATTACHMENT_CACHE_DIR / f"{key}{src.suffix}"
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                log.debug("copy failed for %s: %s", src, exc)
                return None
        kind = "file"
    return {"path": str(dst), "kind": kind, "name": entry.get("name") or src.name}


def _attachment_tag(a: dict) -> str:
    return f"[{a['kind']}: {a['path']}]"


# Message body text lives in `attributedBody` (a NeXT `streamtyped` archive) when
# the plain `text` column is null. The readable string follows the "NSString"
# class marker and a short framing sequence ending in '+', then a length prefix.
def _decode_attributed_body(blob: bytes | None) -> str | None:
    if not blob:
        return None
    marker = blob.find(b"NSString")
    if marker == -1:
        return None
    plus = blob.find(b"+", marker)
    if plus == -1 or plus + 1 >= len(blob):
        return None
    q = plus + 1
    length = blob[q]
    q += 1
    if length == 0x81:  # 2-byte little-endian length
        length = int.from_bytes(blob[q : q + 2], "little")
        q += 2
    elif length == 0x82:  # 4-byte little-endian length
        length = int.from_bytes(blob[q : q + 4], "little")
        q += 4
    text = blob[q : q + length].decode("utf-8", errors="replace")
    return text or None


def _body(row: sqlite3.Row, atts: list[dict] | None = None) -> str:
    text = row["text"] or _decode_attributed_body(row["attributedBody"]) or ""
    tags = " ".join(_attachment_tag(a) for a in (atts or []))
    if text and tags:
        return f"{text} {tags}"
    if tags:
        return tags
    if text:
        return text
    if row["cache_has_attachments"]:
        return "[attachment: unavailable]"
    return ""


def _target_guid(raw: str | None) -> str | None:
    """Bare message guid a tapback points at.

    Messages writes the target three ways — `p:<part>/<guid>` (a part of a
    multi-part message), `bp:<guid>`, and the bare guid — all of which denote
    the same `message.guid`.
    """
    if not raw:
        return None
    return raw.rsplit("/", 1)[-1].split(":")[-1] or None


def _reaction_kind(row: sqlite3.Row) -> str | None:
    """Emoji for a tapback row, or None if the type is not a known reaction."""
    atype = row["associated_message_type"] or 0
    if not _REACTION_ADD_BASE <= atype <= _REACTION_MAX:
        return None
    kind = atype % 1000
    if kind == _REACTION_EMOJI_KIND:
        return row["associated_message_emoji"] or "❔"
    return _REACTION_KINDS.get(kind)


def _fetch_reactions(
    conn: sqlite3.Connection, contacts: dict[str, str], chat_id: int | None = None
) -> dict[str, list[dict]]:
    """Live tapbacks per target guid, in chronological order.

    Adds and removals are replayed in date order so a tapback the sender later
    took back leaves nothing behind — the DB keeps both rows forever.
    """
    scope = "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID" if chat_id else ""
    where = "AND cmj.chat_id = ?" if chat_id else ""
    params: list = [APPLE_EPOCH, _REACTION_ADD_BASE, _REACTION_MAX]
    if chat_id:
        params.append(chat_id)
    rows = conn.execute(
        f"""
        SELECT m.date AS date,
               datetime(m.date/1000000000 + ?, 'unixepoch', 'localtime') AS ts,
               m.is_from_me AS mine, h.id AS handle,
               m.associated_message_guid, m.associated_message_type,
               m.associated_message_emoji
        FROM message m
        {scope}
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.associated_message_type BETWEEN ? AND ?
        {where}
        ORDER BY m.date
        """,
        params,
    ).fetchall()

    live: dict[tuple, dict] = {}
    for r in rows:
        target = _target_guid(r["associated_message_guid"])
        kind = _reaction_kind(r)
        if not target or not kind:
            continue
        sender = "me" if r["mine"] else _resolve(r["handle"], contacts)
        key = (target, sender, kind)
        if r["associated_message_type"] >= _REACTION_REMOVE_BASE:
            live.pop(key, None)
        else:
            live[key] = {"ts": r["ts"], "date": r["date"], "sender": sender, "kind": kind}

    out: dict[str, list[dict]] = {}
    for (target, _, _), entry in sorted(live.items(), key=lambda kv: kv[1]["date"]):
        out.setdefault(target, []).append(entry)
    return out


def _reaction_tag(reactions: list[dict]) -> str:
    return "[" + ", ".join(f"{r['kind']} {r['sender']}" for r in reactions) + "]"


def _target_snippet(conn: sqlite3.Connection, guid: str) -> str:
    """Short quote of the message a tapback points at, for out-of-window targets."""
    row = conn.execute(
        "SELECT text, attributedBody, cache_has_attachments FROM message WHERE guid = ?",
        (guid,),
    ).fetchone()
    if row is None:
        return "(message unavailable)"
    body = " ".join(_body(row).split())
    if len(body) > _TARGET_SNIPPET_CHARS:
        body = body[: _TARGET_SNIPPET_CHARS - 1] + "…"
    return body or "(empty message)"


def _digits(handle: str) -> str:
    """Last 10 digits of a phone handle, for tolerant cross-format matching."""
    return re.sub(r"\D", "", handle)[-10:]


def _contact_map() -> dict[str, str]:
    """Best-effort handle → display name map from the Contacts framework.

    Returns an empty map (never raises) if Contacts access is unavailable, so
    message reading degrades to showing raw handles.
    """
    mapping: dict[str, str] = {}
    try:
        import Contacts  # pyobjc

        store = Contacts.CNContactStore.alloc().init()
        keys = [
            Contacts.CNContactGivenNameKey,
            Contacts.CNContactFamilyNameKey,
            Contacts.CNContactPhoneNumbersKey,
            Contacts.CNContactEmailAddressesKey,
        ]
        request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        collected: list = []

        def _handler(contact, _stop):
            name = " ".join(
                p for p in [contact.givenName(), contact.familyName()] if p
            ).strip()
            if not name:
                return
            for phone in contact.phoneNumbers():
                num = phone.value().stringValue()
                mapping.setdefault(_digits(num), name)
            for email in contact.emailAddresses():
                mapping.setdefault(str(email.value()).lower(), name)
            collected.append(contact)

        store.enumerateContactsWithFetchRequest_error_usingBlock_(
            request, None, _handler
        )
    except Exception as exc:  # noqa: BLE001 — Contacts is optional, never fatal
        log.debug("contact resolution unavailable: %s", exc)
    return mapping


def _resolve(handle: str, contacts: dict[str, str]) -> str:
    if not handle:
        return "me"
    if "@" in handle:
        return contacts.get(handle.lower(), handle)
    return contacts.get(_digits(handle), handle)


def _chats(conn: sqlite3.Connection) -> list[dict]:
    """All chats with their member handles and last-activity timestamp."""
    rows = conn.execute(
        """
        SELECT c.ROWID AS id, c.chat_identifier AS ident, c.display_name AS display,
               MAX(m.date) AS last_date,
               datetime(MAX(m.date)/1000000000 + ?, 'unixepoch', 'localtime') AS last_fmt
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        ORDER BY last_date DESC
        """,
        (APPLE_EPOCH,),
    ).fetchall()
    handles: dict[int, list[str]] = {}
    for r in conn.execute(
        """
        SELECT chj.chat_id AS cid, h.id AS handle
        FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id
        """
    ):
        handles.setdefault(r["cid"], []).append(r["handle"])
    return [
        {
            "id": r["id"],
            "ident": r["ident"],
            "display": r["display"],
            "last_fmt": r["last_fmt"],
            "handles": handles.get(r["id"], []),
        }
        for r in rows
    ]


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, epilog="\b\nExamples:\n  imsg chats --limit 20\n  imsg read \"Jane Doe\" --match 1\n  imsg search invoice --json")
def cli() -> None:
    """Read your local iMessage / SMS history (read-only)."""


@cli.command(epilog="\b\nExamples:\n  imsg chats\n  imsg chats --limit 50 --json")
@click.option("--limit", default=30, help="Number of conversations to list.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def chats(limit: int, as_json: bool) -> None:
    """List conversations, most recent first."""
    conn = _connect()
    contacts = _contact_map()
    rows = _chats(conn)[:limit]
    out = []
    for c in rows:
        name = c["display"] or ", ".join(
            _resolve(h, contacts) for h in c["handles"]
        ) or c["ident"]
        out.append({"last": c["last_fmt"], "name": name, "ident": c["ident"]})
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False))
        return
    for c in out:
        click.echo(f"{c['last']}  {c['name']}")


@cli.command(epilog="\b\nExamples:\n  imsg read \"Jane Doe\" --match 1\n  imsg read \"+33612345678\" --limit 200\n  imsg read Family --json")
@click.argument("query")
@click.option("--limit", default=50, help="Number of messages to show.")
@click.option("--match", default=0, help="Pick the Nth match when ambiguous (1-based).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def read(query: str, limit: int, match: int, as_json: bool) -> None:
    """Read messages from the chat best matching QUERY (name, handle, or group)."""
    conn = _connect()
    contacts = _contact_map()
    needle = query.lower()

    def matches(c: dict) -> bool:
        fields = [c["display"] or "", c["ident"] or "", *c["handles"]]
        fields += [_resolve(h, contacts) for h in c["handles"]]
        return any(needle in f.lower() for f in fields)

    candidates = [c for c in _chats(conn) if matches(c)]
    if not candidates:
        raise click.ClickException(f"No conversation matches {query!r}.")
    if len(candidates) > 1 and not match:
        click.echo(f"Multiple matches for {query!r} — pick with --match N:", err=True)
        for i, c in enumerate(candidates[:20], 1):
            label = c["display"] or ", ".join(
                _resolve(h, contacts) for h in c["handles"]
            ) or c["ident"]
            click.echo(f"  {i}. {label}  ({c['last_fmt']})", err=True)
        raise SystemExit(1)
    chat = candidates[match - 1 if match else 0]

    # Tapback rows are excluded here and folded back into their target below,
    # so --limit counts real messages rather than being eaten by reactions.
    rows = conn.execute(
        """
        SELECT m.ROWID AS mid, m.guid AS guid, m.date AS date,
               datetime(m.date/1000000000 + ?, 'unixepoch', 'localtime') AS ts,
               m.is_from_me AS mine, h.id AS handle,
               m.text, m.attributedBody, m.cache_has_attachments
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
          AND m.associated_message_type NOT BETWEEN ? AND ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (APPLE_EPOCH, chat["id"], _REACTION_ADD_BASE, _REACTION_MAX, limit),
    ).fetchall()
    rows = list(reversed(rows))

    _sweep_stale_attachment_cache()
    raw_atts = _fetch_attachments(conn, [r["mid"] for r in rows])
    reactions = _fetch_reactions(conn, contacts, chat["id"])
    out = []
    for r in rows:
        sender = "me" if r["mine"] else _resolve(r["handle"], contacts)
        atts = [a for a in (_resolve_attachment(e) for e in raw_atts.get(r["mid"], [])) if a]
        entry = {"ts": r["ts"], "sender": sender, "text": _body(r, atts)}
        if atts:
            entry["attachments"] = atts
        if reactions.get(r["guid"]):
            entry["reactions"] = [
                {"kind": x["kind"], "sender": x["sender"]} for x in reactions[r["guid"]]
            ]
        out.append(entry)

    # A tapback on a message older than the window has nothing to attach to, so
    # it keeps its own line — quoting the target rather than dropping it.
    shown = {r["guid"] for r in rows}
    floor = rows[0]["date"] if rows else 0
    for target, entries in reactions.items():
        if target in shown:
            continue
        for x in entries:
            if x["date"] < floor:
                continue
            snippet = _target_snippet(conn, target)
            out.append(
                {
                    "ts": x["ts"],
                    "sender": x["sender"],
                    "text": f"{x['kind']} → “{snippet}”",
                    "reaction": x["kind"],
                    "reaction_target": snippet,
                }
            )
    out.sort(key=lambda m: m["ts"])
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False))
        return
    title = chat["display"] or ", ".join(
        _resolve(h, contacts) for h in chat["handles"]
    ) or chat["ident"]
    click.echo(f"# {title} — {len(out)} messages\n")
    for m in out:
        tag = f"  {_reaction_tag(m['reactions'])}" if m.get("reactions") else ""
        click.echo(f"{m['ts']}  {m['sender']}: {m['text']}{tag}")


@cli.command(epilog="\b\nExamples:\n  imsg search invoice\n  imsg search invoice --limit 20 --json")
@click.argument("text")
@click.option("--limit", default=50, help="Number of matches to show.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def search(text: str, limit: int, as_json: bool) -> None:
    """Full-text search across all messages."""
    conn = _connect()
    contacts = _contact_map()
    rows = conn.execute(
        """
        SELECT m.ROWID AS mid, m.guid AS guid,
               datetime(m.date/1000000000 + ?, 'unixepoch', 'localtime') AS ts,
               m.is_from_me AS mine, h.id AS handle, m.text, m.attributedBody,
               m.cache_has_attachments
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.text LIKE ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (APPLE_EPOCH, f"%{text}%", limit),
    ).fetchall()
    _sweep_stale_attachment_cache()
    raw_atts = _fetch_attachments(conn, [r["mid"] for r in rows])
    reactions = _fetch_reactions(conn, contacts)
    out = []
    for r in rows:
        atts = [a for a in (_resolve_attachment(e) for e in raw_atts.get(r["mid"], [])) if a]
        entry = {
            "ts": r["ts"],
            "sender": "me" if r["mine"] else _resolve(r["handle"], contacts),
            "text": _body(r, atts),
        }
        if atts:
            entry["attachments"] = atts
        if reactions.get(r["guid"]):
            entry["reactions"] = [
                {"kind": x["kind"], "sender": x["sender"]} for x in reactions[r["guid"]]
            ]
        out.append(entry)
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False))
        return
    for m in out:
        tag = f"  {_reaction_tag(m['reactions'])}" if m.get("reactions") else ""
        click.echo(f"{m['ts']}  {m['sender']}: {m['text']}{tag}")


def _applescript_quote(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    # AppleScript string literals can't span raw newlines; splice in `linefeed`.
    return escaped.replace("\n", '" & linefeed & "')


def _matching_chats(conn: sqlite3.Connection, needle: str, contacts: dict) -> list[dict]:
    def hit(c: dict) -> bool:
        fields = [c["display"] or "", c["ident"] or "", *c["handles"]]
        fields += [_resolve(h, contacts) for h in c["handles"]]
        return any(needle in f.lower() for f in fields)

    return [c for c in _chats(conn) if hit(c)]


@cli.command(epilog="\b\nExamples:\n  imsg send \"Jane Doe\" \"running late\" --match 1\n  imsg send x \"running late\" --handle +33612345678 --yes\n  imsg send \"Jane Doe\" \"on my way\" --match 1 --sms --yes")
@click.argument("query")
@click.argument("text")
@click.option("--handle", "handle_override", help="Send to this exact handle, skipping query resolution.")
@click.option("--match", default=0, help="Pick the Nth matching chat when ambiguous (1-based).")
@click.option("--yes", is_flag=True, help="Actually send. Without it, prints a dry-run only.")
@click.option("--sms", is_flag=True, help="Send over SMS instead of iMessage.")
def send(
    query: str, text: str, handle_override: str | None, match: int, yes: bool, sms: bool
) -> None:
    """Send an iMessage/SMS to the contact matching QUERY.

    Defaults to a dry-run that prints the resolved recipient and text without
    sending. Pass --yes to actually send — an outbound action the calling agent
    must confirm with the user first.
    """
    if handle_override:
        handle = handle_override
        label = handle
    else:
        conn = _connect()
        contacts = _contact_map()
        # Only 1:1 chats (a single member handle) have an unambiguous recipient.
        cands = [
            c
            for c in _matching_chats(conn, query.lower(), contacts)
            if len(c["handles"]) == 1
        ]
        if not cands:
            raise click.ClickException(
                f"No 1:1 conversation matches {query!r}. Pass --handle to be explicit."
            )
        if len(cands) > 1 and not match:
            click.echo(f"Multiple matches for {query!r} — pick with --match N:", err=True)
            for i, c in enumerate(cands[:20], 1):
                click.echo(
                    f"  {i}. {_resolve(c['handles'][0], contacts)}  <{c['handles'][0]}>",
                    err=True,
                )
            raise SystemExit(1)
        chosen = cands[match - 1 if match else 0]
        handle = chosen["handles"][0]
        label = _resolve(handle, contacts)

    service = "SMS" if sms else "iMessage"
    if not yes:
        click.echo("DRY-RUN (nothing sent). Re-run with --yes to send.")
        click.echo(f"  to:      {label} <{handle}>  via {service}")
        click.echo(f"  message: {text}")
        return

    script = (
        'tell application "Messages"\n'
        f"  set svc to 1st account whose service type = {service}\n"
        f'  set theBuddy to participant "{handle}" of svc\n'
        f'  send "{_applescript_quote(text)}" to theBuddy\n'
        "end tell"
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise click.ClickException(result.stderr.strip() or "osascript send failed")
    click.echo(f"Sent to {label} <{handle}> via {service}.")


def _launch_agent_plist() -> dict:
    """LaunchAgent that keeps the helper running in the user's session."""
    return {
        "Label": HELPER_BUNDLE_ID,
        "ProgramArguments": [str(HELPER_INSTALLED_APP / "Contents" / "MacOS" / HELPER_EXECUTABLE)],
        # Shows the helper under its app name in Login Items, not as a bare binary.
        "AssociatedBundleIdentifiers": [HELPER_BUNDLE_ID],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(HELPER_LOG),
        "StandardErrorPath": str(HELPER_LOG),
    }


def _launchd_target() -> str:
    return f"gui/{os.getuid()}"


def _agent_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "print", f"{_launchd_target()}/{HELPER_BUNDLE_ID}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _helper_status() -> dict:
    """Installed app, LaunchAgent, the copy's age, and the helper's last report."""
    reported = _helper_report()
    copy_age = time.time() - HELPER_COPY.stat().st_mtime if HELPER_COPY.is_file() else None
    status = {
        "direct_access": _readable(CHAT_DB),
        "app_installed": HELPER_INSTALLED_APP.is_dir(),
        "agent_installed": LAUNCH_AGENT.is_file(),
        "agent_loaded": _agent_loaded(),
        "copy": str(HELPER_COPY),
        "copy_age_seconds": None if copy_age is None else round(copy_age),
        "helper_ok": reported.get("ok"),
        "helper_error": reported.get("error"),
        "helper_checked_seconds_ago": (
            round(time.time() - reported["checked_at"]) if reported.get("checked_at") else None
        ),
    }
    status["ready"] = status["direct_access"] or (
        copy_age is not None and _helper_current(reported)
    )
    return status


def _next_step(status: dict) -> str:
    """The one action that moves the setup forward, or an empty string.

    With direct access and no helper there is nothing to do; once the helper is
    installed its own setup is still reported.
    """
    if status["direct_access"] and not status["app_installed"]:
        return ""
    if not status["app_installed"] or not status["agent_loaded"]:
        return "Run `imsg helper-install --yes`."
    if status["helper_ok"] is False:
        return (
            "In System Settings → Privacy & Security → Full Disk Access, switch on "
            f"\"iMessage Reader\" (drag {HELPER_INSTALLED_APP} into the list if it is "
            "missing). The helper retries every few seconds; no restart needed."
        )
    if status["helper_ok"] is None:
        return "The helper has not reported yet: check again in a few seconds."
    if not status["ready"]:
        return f"The helper stopped reporting: check {HELPER_LOG}."
    return ""


def _bundled_app() -> Path:
    """The prebuilt helper app, built from source when absent and swiftc exists."""
    if HELPER_BUILT_APP.is_dir():
        return HELPER_BUILT_APP
    if shutil.which("swiftc") is None:
        raise click.ClickException(
            f"No prebuilt {HELPER_APP_NAME} at {HELPER_BUILT_APP} and no Swift "
            "compiler to build it: install the Xcode command line tools "
            "(`xcode-select --install`) or use an extension that bundles the app."
        )
    subprocess.run(
        [sys.executable, str(HELPER_SOURCE_DIR / "build.py")],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return HELPER_BUILT_APP


@cli.command("helper-install", epilog="\b\nExamples:\n  imsg helper-install\n  imsg helper-install --yes")
@click.option("--yes", is_flag=True, help="Install. Without it, only list the steps.")
def helper_install(yes: bool) -> None:
    """Install the iMessage Reader helper, the only app needing Full Disk Access.

    Copies the helper app to ~/Applications, registers it as a LaunchAgent so it
    keeps a private copy of chat.db fresh, then opens the Full Disk Access
    settings and shows the app in Finder: the user switches it on there.
    """
    steps = [
        f"copy {HELPER_APP_NAME} to {HELPER_INSTALLED_APP}",
        f"write {LAUNCH_AGENT} and load it with launchctl",
        "open System Settings → Privacy & Security → Full Disk Access",
        f"show {HELPER_INSTALLED_APP} in Finder",
    ]
    if not yes:
        click.echo("DRY-RUN (nothing installed). Re-run with --yes to:")
        for step in steps:
            click.echo(f"  - {step}")
        return
    app = _bundled_app()
    HELPER_INSTALLED_APP.parent.mkdir(parents=True, exist_ok=True)
    if HELPER_INSTALLED_APP.exists():
        shutil.rmtree(HELPER_INSTALLED_APP)
    shutil.copytree(app, HELPER_INSTALLED_APP, symlinks=True)
    # Archive extraction can drop the exec bit; the mode is not part of the signature.
    (HELPER_INSTALLED_APP / "Contents" / "MacOS" / HELPER_EXECUTABLE).chmod(0o755)
    HELPER_DIR.mkdir(parents=True, exist_ok=True)
    HELPER_DIR.chmod(0o700)
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps(_launch_agent_plist()))
    # bootstrap refuses a label that is already loaded, so a reinstall unloads first.
    subprocess.run(
        ["launchctl", "bootout", f"{_launchd_target()}/{HELPER_BUNDLE_ID}"],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", _launchd_target(), str(LAUNCH_AGENT)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"launchctl bootstrap failed: {result.stderr.strip()}")
    subprocess.run(["open", FDA_PANE_URL], check=False)
    subprocess.run(["open", "-R", str(HELPER_INSTALLED_APP)], check=False)
    click.echo(f"Installed {HELPER_INSTALLED_APP} and started it.")
    click.echo(
        "Last step, in the Full Disk Access window that just opened: switch on "
        "\"iMessage Reader\", or drag it from the Finder window into the list. "
        "Then check with `imsg helper-status`."
    )


@cli.command("helper-uninstall", epilog="\b\nExamples:\n  imsg helper-uninstall\n  imsg helper-uninstall --yes")
@click.option("--yes", is_flag=True, help="Uninstall. Without it, only list the steps.")
def helper_uninstall(yes: bool) -> None:
    """Stop and remove the iMessage Reader helper and its copy of chat.db."""
    if not yes:
        click.echo("DRY-RUN (nothing removed). Re-run with --yes to remove:")
        for path in (LAUNCH_AGENT, HELPER_INSTALLED_APP, HELPER_DIR):
            click.echo(f"  - {path}")
        return
    subprocess.run(
        ["launchctl", "bootout", f"{_launchd_target()}/{HELPER_BUNDLE_ID}"],
        capture_output=True,
    )
    LAUNCH_AGENT.unlink(missing_ok=True)
    shutil.rmtree(HELPER_INSTALLED_APP, ignore_errors=True)
    shutil.rmtree(HELPER_DIR, ignore_errors=True)
    click.echo(
        "Removed. Its Full Disk Access entry stays in System Settings until you "
        "remove it there (select it, then −)."
    )


@cli.command("helper-status", epilog="\b\nExamples:\n  imsg helper-status\n  imsg helper-status --json")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def helper_status(as_json: bool) -> None:
    """Whether messages are readable, directly or through the iMessage Reader helper."""
    status = _helper_status()
    status["next_step"] = _next_step(status)
    if as_json:
        click.echo(json.dumps(status, ensure_ascii=False))
        return
    age = status["copy_age_seconds"]
    click.echo(f"ready:           {'yes' if status['ready'] else 'no'}")
    click.echo(f"direct access:   {'yes' if status['direct_access'] else 'no'}")
    click.echo(f"helper app:      {'installed' if status['app_installed'] else 'missing'}")
    click.echo(f"launch agent:    {'running' if status['agent_loaded'] else 'not loaded'}")
    click.echo(f"copy:            {'none' if age is None else f'written {age} s ago'}")
    checked = status["helper_checked_seconds_ago"]
    click.echo(f"helper checked:  {'never' if checked is None else f'{checked} s ago'}")
    if status["helper_error"]:
        click.echo(f"helper error:    {status['helper_error']}")
    if status["next_step"]:
        click.echo(f"next step:       {status['next_step']}")


if __name__ == "__main__":
    cli()
