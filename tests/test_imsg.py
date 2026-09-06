"""Tests for tapback (reaction) folding in the `imsg` CLI.

The CLI is a PEP 723 single-file script without a `.py` extension, so it is
loaded by path rather than imported. Fixtures build a miniature chat.db in
memory: only the columns the CLI actually queries are declared.

Run with: uv run --with pytest --with click pytest tests/
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

_SPEC = importlib.util.spec_from_loader(
    "imsg",
    importlib.machinery.SourceFileLoader(
        "imsg", str(Path(__file__).resolve().parent.parent / "bin" / "imsg")
    ),
)
imsg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(imsg)

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT, transfer_name TEXT);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB,
    handle_id INTEGER, is_from_me INTEGER, date INTEGER,
    cache_has_attachments INTEGER DEFAULT 0,
    associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
    associated_message_emoji TEXT
);
"""

# Apple absolute time in nanoseconds; the exact instant is irrelevant, only order.
T0 = 700_000_000 * 1_000_000_000


def _insert(conn, rowid, guid, *, text=None, mine=0, offset=0, assoc=None, atype=0, emoji=None):
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, handle_id, is_from_me, date,"
        " associated_message_guid, associated_message_type, associated_message_emoji)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (rowid, guid, text, 1, mine, T0 + offset * 1_000_000_000, assoc, atype, emoji),
    )
    conn.execute("INSERT INTO chat_message_join VALUES (1, ?)", (rowid,))


@pytest.fixture
def db():
    """Chat with two messages: 'hello' liked by the peer, 'bye' un-reacted."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO handle VALUES (1, '+33600000000')")
    conn.execute("INSERT INTO chat VALUES (1, '+33600000000', NULL)")
    conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
    _insert(conn, 1, "G-HELLO", text="hello", mine=1, offset=0)
    _insert(conn, 2, "G-BYE", text="bye", mine=1, offset=10)
    _insert(conn, 3, "R-LIKE", offset=20, assoc="p:0/G-HELLO", atype=2001)
    return conn


@pytest.fixture
def run(db, monkeypatch):
    """Invoke the CLI against the fixture DB with contact lookup stubbed out."""
    monkeypatch.setattr(imsg, "_connect", lambda: db)
    monkeypatch.setattr(imsg, "_contact_map", dict)

    def _run(*args):
        result = CliRunner().invoke(imsg.cli, list(args), catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    return _run


@pytest.mark.parametrize(
    "raw",
    ["p:0/G-HELLO", "p:1/G-HELLO", "bp:G-HELLO", "G-HELLO"],
    ids=["part-0", "part-1", "bp", "bare"],
)
def test_target_guid_normalizes_every_form(raw):
    assert imsg._target_guid(raw) == "G-HELLO"


def test_target_guid_of_none_is_none():
    assert imsg._target_guid(None) is None


@pytest.mark.parametrize(
    ("atype", "kind"),
    [(2000, "❤️"), (2001, "👍"), (2002, "👎"), (2003, "😂"), (2004, "‼️"), (2005, "❓")],
)
def test_reaction_kind_maps_builtin_tapbacks(atype, kind):
    row = {"associated_message_type": atype, "associated_message_emoji": None}
    assert imsg._reaction_kind(row) == kind


def test_reaction_kind_uses_emoji_column_for_free_form_tapback():
    row = {"associated_message_type": 2006, "associated_message_emoji": "🎉"}
    assert imsg._reaction_kind(row) == "🎉"


def test_reaction_kind_of_plain_message_is_none():
    row = {"associated_message_type": 0, "associated_message_emoji": None}
    assert imsg._reaction_kind(row) is None


def test_read_attaches_reaction_to_its_target(run):
    assert "hello  [👍 +33600000000]" in run("read", "+336")


def test_read_leaves_unreacted_message_untagged(run):
    assert "me: bye\n" in run("read", "+336")


def test_read_does_not_list_the_tapback_as_a_message(run):
    assert run("read", "+336").startswith("# +33600000000 — 2 messages")


def test_read_drops_reaction_taken_back(db, run):
    _insert(db, 4, "R-UNLIKE", offset=30, assoc="p:0/G-HELLO", atype=3001)
    assert "👍" not in run("read", "+336")


def test_read_keeps_reaction_re_added_after_removal(db, run):
    _insert(db, 4, "R-UNLIKE", offset=30, assoc="p:0/G-HELLO", atype=3001)
    _insert(db, 5, "R-RELIKE", offset=40, assoc="p:0/G-HELLO", atype=2001)
    assert "hello  [👍 +33600000000]" in run("read", "+336")


def test_read_keeps_distinct_reactions_from_the_same_sender(db, run):
    _insert(db, 4, "R-LAUGH", offset=30, assoc="p:0/G-HELLO", atype=2003)
    assert "hello  [👍 +33600000000, 😂 +33600000000]" in run("read", "+336")


def test_read_quotes_target_of_reaction_older_than_the_window(run):
    assert '👍 → “hello”' in run("read", "+336", "--limit", "1")


def test_read_reports_unavailable_target(db, run):
    _insert(db, 4, "R-GHOST", offset=30, assoc="p:0/G-GONE", atype=2000)
    assert "❤️ → “(message unavailable)”" in run("read", "+336")


def test_read_json_carries_reactions(run):
    import json

    assert json.loads(run("read", "+336", "--json"))[0]["reactions"] == [
        {"kind": "👍", "sender": "+33600000000"}
    ]


def test_search_attaches_reactions_to_hits(run):
    assert "hello  [👍 +33600000000]" in run("search", "hello")
