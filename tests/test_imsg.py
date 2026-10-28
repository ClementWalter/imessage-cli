#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "pyobjc-framework-Contacts==12.1; sys_platform == 'darwin'",
#     "pytest==9.1.1",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Tests for the `imsg` CLI: tapback folding and the iMessage Reader helper.

The CLI is a PEP 723 single-file script, loaded by path rather than imported.
Fixtures build a miniature chat.db in memory: only the columns the CLI actually
queries are declared. Helper tests point every helper path at a temp dir and
stub `subprocess.run`, so nothing touches launchd, System Settings or the real
Messages database.

Run with: uv run --script tests/test_imsg.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

_SPEC = importlib.util.spec_from_loader(
    "imsg",
    importlib.machinery.SourceFileLoader(
        "imsg", str(Path(__file__).resolve().parent.parent / "imessage_cli.py")
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
    assert json.loads(run("read", "+336", "--json"))[0]["reactions"] == [
        {"kind": "👍", "sender": "+33600000000"}
    ]


def test_search_attaches_reactions_to_hits(run):
    assert "hello  [👍 +33600000000]" in run("search", "hello")



# --- iMessage Reader helper -------------------------------------------------


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Every path the CLI uses for chat.db and the helper, under tmp_path."""
    p = {
        "chat_db": tmp_path / "Messages" / "chat.db",
        "helper_dir": tmp_path / "support",
        "built_app": tmp_path / "dist" / "iMessage Reader.app",
        "installed_app": tmp_path / "Applications" / "iMessage Reader.app",
        "agent": tmp_path / "LaunchAgents" / "agent.plist",
    }
    monkeypatch.setattr(imsg, "CHAT_DB", p["chat_db"])
    monkeypatch.setattr(imsg, "HELPER_DIR", p["helper_dir"])
    monkeypatch.setattr(imsg, "HELPER_COPY", p["helper_dir"] / "chat.db")
    monkeypatch.setattr(imsg, "HELPER_STATUS", p["helper_dir"] / "status.json")
    monkeypatch.setattr(imsg, "HELPER_LOG", p["helper_dir"] / "helper.log")
    monkeypatch.setattr(imsg, "HELPER_BUILT_APP", p["built_app"])
    monkeypatch.setattr(imsg, "HELPER_INSTALLED_APP", p["installed_app"])
    monkeypatch.setattr(imsg, "LAUNCH_AGENT", p["agent"])
    return p


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def no_direct_access(paths, monkeypatch):
    """chat.db exists but is unreadable, as for a process without Full Disk Access."""
    _write(paths["chat_db"], "live")
    real_readable = imsg._readable
    monkeypatch.setattr(imsg, "_readable", lambda p: p != paths["chat_db"] and real_readable(p))


@pytest.fixture
def helper_copy(paths):
    return _write(paths["helper_dir"] / "chat.db", "copy")


def _report(paths, *, ok=True, age=0.0, error=None):
    status = {"ok": ok, "checked_at": time.time() - age, "error": error}
    _write(paths["helper_dir"] / "status.json", json.dumps(status))


@pytest.fixture
def calls(monkeypatch):
    """Records every subprocess.run command; each one succeeds."""
    recorded: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_):
        recorded.append([str(c) for c in cmd])
        return Done()

    monkeypatch.setattr(imsg.subprocess, "run", fake_run)
    return recorded


@pytest.fixture
def built_app(paths):
    """A stand-in for the built bundle: only the files install copies."""
    app = paths["built_app"]
    _write(app / "Contents" / "Info.plist", "plist")
    _write(app / "Contents" / "MacOS" / imsg.HELPER_EXECUTABLE, "binary")
    return app


def test_db_source_prefers_readable_chat_db(paths, helper_copy):
    _write(paths["chat_db"], "live")
    assert imsg._db_source() == paths["chat_db"]


def test_db_source_falls_back_to_helper_copy(no_direct_access, helper_copy):
    assert imsg._db_source() == helper_copy


def test_snapshot_of_helper_copy_is_named_chat_db(no_direct_access, helper_copy):
    assert imsg._snapshot_db().read_text() == "copy"


def test_db_source_without_any_copy_points_at_helper_install(no_direct_access):
    with pytest.raises(imsg.click.ClickException, match="helper-install"):
        imsg._db_source()


def test_db_source_warns_when_helper_stopped_reporting(paths, no_direct_access, helper_copy, caplog):
    _report(paths, age=imsg.HELPER_STALE_SECONDS + 60)
    imsg._db_source()
    assert "helper-status" in caplog.text


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("Label", "com.github.clementwalter.imessage-reader"),
        ("AssociatedBundleIdentifiers", ["com.github.clementwalter.imessage-reader"]),
        ("RunAtLoad", True),
        ("KeepAlive", True),
        ("ProcessType", "Background"),
    ],
)
def test_launch_agent_plist_entry(paths, key, expected):
    assert imsg._launch_agent_plist()[key] == expected


def test_launch_agent_runs_installed_executable(paths):
    assert imsg._launch_agent_plist()["ProgramArguments"] == [
        str(paths["installed_app"] / "Contents" / "MacOS" / "imessage-reader")
    ]


def test_launch_agent_plist_serializes(paths):
    assert plistlib.loads(plistlib.dumps(imsg._launch_agent_plist()))["Label"] == imsg.HELPER_BUNDLE_ID


def test_status_ready_when_helper_reports(paths, no_direct_access, helper_copy, calls):
    _report(paths)
    assert imsg._helper_status()["ready"] is True


def test_status_ready_with_old_copy_of_idle_database(paths, no_direct_access, helper_copy, calls):
    old = time.time() - 3600
    os.utime(helper_copy, (old, old))
    _report(paths)
    assert imsg._helper_status()["ready"] is True


def test_status_not_ready_when_helper_stopped_reporting(paths, no_direct_access, helper_copy, calls):
    _report(paths, age=imsg.HELPER_STALE_SECONDS + 60)
    assert imsg._helper_status()["ready"] is False


def test_status_not_ready_when_helper_denied(paths, no_direct_access, helper_copy, calls):
    _report(paths, ok=False, error="no Full Disk Access")
    assert imsg._helper_status()["ready"] is False


def test_status_reports_helper_error(no_direct_access, paths, calls):
    _report(paths, ok=False, error="no Full Disk Access")
    assert imsg._helper_status()["helper_error"] == "no Full Disk Access"


def test_status_tolerates_corrupt_status_file(no_direct_access, paths, calls):
    _write(paths["helper_dir"] / "status.json", "{")
    assert imsg._helper_status()["helper_ok"] is None


def _status(**overrides):
    return {"direct_access": False, "app_installed": True, "agent_loaded": True,
            "helper_ok": False, "ready": False, **overrides}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"app_installed": False}, "helper-install"),
        ({"agent_loaded": False}, "helper-install"),
        ({"helper_ok": False}, "Full Disk Access"),
        ({"helper_ok": None}, "not reported"),
        ({"helper_ok": True, "ready": False}, "stopped reporting"),
    ],
    ids=["no-app", "no-agent", "denied", "starting", "stale"],
)
def test_next_step_names_the_fix(overrides, expected):
    assert expected in imsg._next_step(_status(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [{"direct_access": True, "app_installed": False}, {"helper_ok": True, "ready": True}],
    ids=["direct", "ready"],
)
def test_next_step_is_empty_when_readable(overrides):
    assert imsg._next_step(_status(**overrides)) == ""


def test_helper_status_json_carries_next_step(no_direct_access, calls):
    result = CliRunner().invoke(imsg.cli, ["helper-status", "--json"])
    assert "next_step" in json.loads(result.output)


def test_helper_install_dry_run_installs_nothing(paths, built_app, calls):
    CliRunner().invoke(imsg.cli, ["helper-install"])
    assert not paths["installed_app"].exists()


def test_helper_install_dry_run_runs_no_command(paths, built_app, calls):
    CliRunner().invoke(imsg.cli, ["helper-install"])
    assert calls == []


@pytest.fixture
def installed(paths, built_app, calls):
    result = CliRunner().invoke(imsg.cli, ["helper-install", "--yes"])
    assert result.exit_code == 0, result.output
    return calls


def test_helper_install_copies_app(paths, installed):
    assert (paths["installed_app"] / "Contents" / "MacOS" / "imessage-reader").read_text() == "binary"


def test_helper_install_makes_executable_runnable(paths, installed):
    assert os.access(paths["installed_app"] / "Contents" / "MacOS" / "imessage-reader", os.X_OK)


def test_helper_install_writes_launch_agent(paths, installed):
    assert plistlib.loads(paths["agent"].read_bytes()) == imsg._launch_agent_plist()


def test_helper_install_keeps_copy_folder_private(paths, installed):
    assert paths["helper_dir"].stat().st_mode & 0o777 == 0o700


def test_helper_install_bootstraps_agent(paths, installed):
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths["agent"])] in installed


def test_helper_install_opens_full_disk_access_pane(installed):
    assert ["open", imsg.FDA_PANE_URL] in installed


def test_helper_install_reveals_app_in_finder(paths, installed):
    assert ["open", "-R", str(paths["installed_app"])] in installed


def test_helper_install_without_app_or_compiler_fails(paths, calls, monkeypatch):
    monkeypatch.setattr(imsg.shutil, "which", lambda _: None)
    result = CliRunner().invoke(imsg.cli, ["helper-install", "--yes"])
    assert "xcode-select --install" in result.output


def test_helper_uninstall_removes_app(paths, installed):
    CliRunner().invoke(imsg.cli, ["helper-uninstall", "--yes"])
    assert not paths["installed_app"].exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
