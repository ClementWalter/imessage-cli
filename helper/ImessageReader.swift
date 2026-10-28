// iMessage Reader: the one process that holds Full Disk Access for imessage-cli.
//
// macOS has no privacy grant narrower than Full Disk Access for
// ~/Library/Messages, and a grant to the app that launches imessage-cli (a
// terminal, Claude) would cover everything else that app runs. This helper
// alone holds the grant and does one thing: it keeps a consistent read-only
// copy of the Messages database in a private folder that imessage-cli reads.
//
// What it touches, and nothing else:
//   reads   ~/Library/Messages/chat.db (+ the -wal/-shm SQLite opens with it)
//   writes  ~/Library/Application Support/imessage-cli/ (chmod 700):
//           chat.db (chmod 600) and status.json
// No network, no other files, no arguments besides --once.

import Darwin
import Foundation
import SQLite3

let home = FileManager.default.homeDirectoryForCurrentUser
let source = home.appendingPathComponent("Library/Messages/chat.db")
let outDir = home.appendingPathComponent("Library/Application Support/imessage-cli")
let copyURL = outDir.appendingPathComponent("chat.db")
let statusURL = outDir.appendingPathComponent("status.json")

// Source mtimes are checked often but the ~200 MB copy is rewritten at most
// this often, so a busy conversation does not turn into constant disk writes.
let pollSeconds: UInt32 = 5
let minCopyInterval: TimeInterval = 30

struct CopyError: Error, CustomStringConvertible {
    let description: String
}

func ensureOutDir() throws {
    try FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
    // Re-applied every time: the copy holds every message, so the folder stays owner-only.
    chmod(outDir.path, 0o700)
}

/// Latest mtime across chat.db and its WAL: a new message lands in the WAL first.
func sourceStamp() -> Date? {
    ["", "-wal"].compactMap { suffix in
        try? FileManager.default.attributesOfItem(atPath: source.path + suffix)[.modificationDate] as? Date
    }.max()
}

func removeSidecars(_ db: URL) {
    for suffix in ["-wal", "-shm", "-journal"] {
        try? FileManager.default.removeItem(atPath: db.path + suffix)
    }
}

func removeWithSidecars(_ db: URL) {
    try? FileManager.default.removeItem(at: db)
    removeSidecars(db)
}

/// Copies chat.db through the SQLite backup API into a temp file, then renames it
/// over the published copy so a reader never sees a half-written database.
func copyDatabase() throws {
    // A plain open() surfaces the privacy denial as EPERM, which SQLite would
    // flatten into a generic "unable to open database file".
    let fd = open(source.path, O_RDONLY)
    if fd < 0 {
        let err = errno
        if err == EPERM || err == EACCES {
            throw CopyError(description: "no Full Disk Access: switch on iMessage Reader in System Settings > Privacy & Security > Full Disk Access")
        }
        throw CopyError(description: "cannot open \(source.path): \(String(cString: strerror(err)))")
    }
    close(fd)

    var src: OpaquePointer?
    var dst: OpaquePointer?
    defer {
        sqlite3_close(src)
        sqlite3_close(dst)
    }
    // Read-only, so closing the last connection can never checkpoint or delete
    // Messages' WAL.
    guard sqlite3_open_v2(source.path, &src, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        throw CopyError(description: "open source: \(String(cString: sqlite3_errmsg(src)))")
    }
    sqlite3_busy_timeout(src, 5000)

    let tmp = outDir.appendingPathComponent("chat.db.tmp")
    removeWithSidecars(tmp)
    guard sqlite3_open(tmp.path, &dst) == SQLITE_OK else {
        throw CopyError(description: "open copy: \(String(cString: sqlite3_errmsg(dst)))")
    }
    guard let backup = sqlite3_backup_init(dst, "main", src, "main") else {
        throw CopyError(description: "backup init: \(String(cString: sqlite3_errmsg(dst)))")
    }
    var rc: Int32
    repeat {
        rc = sqlite3_backup_step(backup, -1)
        if rc == SQLITE_BUSY || rc == SQLITE_LOCKED { usleep(200_000) }
    } while rc == SQLITE_OK || rc == SQLITE_BUSY || rc == SQLITE_LOCKED
    sqlite3_backup_finish(backup)
    guard rc == SQLITE_DONE else {
        throw CopyError(description: "backup: \(String(cString: sqlite3_errstr(rc)))")
    }
    // The copy is a single self-contained file: no WAL sidecars to keep in sync.
    sqlite3_exec(dst, "PRAGMA journal_mode=DELETE", nil, nil, nil)
    sqlite3_close(dst)
    dst = nil
    // The backup carries the source's WAL header, so SQLite leaves a -shm behind.
    removeSidecars(tmp)
    chmod(tmp.path, 0o600)
    guard rename(tmp.path, copyURL.path) == 0 else {
        throw CopyError(description: "rename: \(String(cString: strerror(errno)))")
    }
}

func writeStatus(ok: Bool, error: String?, copiedAt: Date?) {
    var status: [String: Any] = [
        "ok": ok,
        "checked_at": Date().timeIntervalSince1970,
        "pid": Int(getpid()),
    ]
    status["error"] = error ?? NSNull()
    status["copied_at"] = copiedAt.map { $0.timeIntervalSince1970 } ?? NSNull()
    guard let data = try? JSONSerialization.data(withJSONObject: status, options: [.sortedKeys]) else { return }
    try? data.write(to: statusURL, options: .atomic)
    chmod(statusURL.path, 0o600)
}

func log(_ message: String) {
    FileHandle.standardError.write(Data("\(ISO8601DateFormatter().string(from: Date())) \(message)\n".utf8))
}

func run(once: Bool) -> Int32 {
    var lastCopy: Date?
    var lastStamp: Date?
    var lastError: String?
    while true {
        do {
            try ensureOutDir()
            let stamp = sourceStamp()
            let changed = stamp == nil || stamp != lastStamp || !FileManager.default.fileExists(atPath: copyURL.path)
            let due = lastCopy.map { Date().timeIntervalSince($0) >= minCopyInterval } ?? true
            if changed && due {
                try copyDatabase()
                lastCopy = Date()
                lastStamp = stamp
                if lastError != nil { log("copy ok") }
                lastError = nil
            }
            writeStatus(ok: true, error: nil, copiedAt: lastCopy)
        } catch {
            let message = String(describing: error)
            // Logged once per distinct failure, not every poll.
            if message != lastError { log(message) }
            lastError = message
            writeStatus(ok: false, error: message, copiedAt: lastCopy)
        }
        if once { return lastError == nil ? 0 : 1 }
        sleep(pollSeconds)
    }
}

exit(run(once: CommandLine.arguments.contains("--once")))
