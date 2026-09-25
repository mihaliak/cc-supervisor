@testable import CCSupervisor
import SwiftUI
import XCTest

final class StatuslinePreviewTests: XCTestCase {
    func testSegmentsBecomeColoredRuns() throws {
        let preview = try CcsClient.decoder.decode(StatuslinePreviewResult.self, from: try Fixtures.data("ccs/statusline_preview.json"))
        XCTAssertEqual(preview.status?.applied, false)
        XCTAssertEqual(preview.status?.scriptPath, "/tmp/cfg-lab/ccs-statusline.py")
        let normal = try XCTUnwrap(preview.samples?.first { $0.state == "normal" })
        let segments = try XCTUnwrap(normal.segments)
        let attributed = SegmentsPreview.attributed(segments)
        XCTAssertEqual(String(attributed.characters), normal.plain)

        let runs = attributed.runs.map { (String(attributed[$0.range].characters), $0.foregroundColor) }
        // Adjacent segments with equal attributes merge into one run, so compare per segment.
        for segment in segments {
            let expected = SegmentsPreview.color(for: segment.color)
            let run = runs.first { $0.0.contains(segment.text) }
            XCTAssertNotNil(run, segment.text)
            XCTAssertEqual(run?.1, expected, "\(segment.text) [\(segment.color ?? "nil")]")
        }
        XCTAssertEqual(SegmentsPreview.color(for: "green"), LevelColor.color(.green))
        XCTAssertEqual(SegmentsPreview.color(for: "gray"), .gray)
        XCTAssertNil(SegmentsPreview.color(for: "plain"))
        XCTAssertTrue(preview.samples?.contains { $0.state == "paused" } ?? false)
    }
}

/// Regression: "Take theirs" was the alert's cancel button, so Esc silently threw away the
/// user's edits. Esc now keeps them; taking the file's values is the destructive choice.
final class ConflictAlertTests: XCTestCase {
    func testEscapeKeepsLocalEdits() {
        let cancel = ConflictChoice.allCases.filter { $0.role == .cancel }
        XCTAssertEqual(cancel, [.keepMine], "exactly one Esc choice, and it keeps local edits")
        XCTAssertEqual(ConflictChoice.keepMine.resolution, .keepMine)
        XCTAssertEqual(ConflictChoice.takeTheirs.role, .destructive)
        XCTAssertEqual(ConflictChoice.takeTheirs.resolution, .takeTheirs)
        XCTAssertEqual(ConflictChoice.allCases.map(\.title), ["Keep mine", "Take theirs"])
    }
}

final class CcsSettingsClientTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-settings-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func script(_ body: String) throws -> String {
        let url = dir.appendingPathComponent("ccs")
        try ("#!/bin/sh\necho \"$@\" > \"\(dir.path)/argv\"\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url.path
    }

    private func argv() throws -> String {
        try String(contentsOf: dir.appendingPathComponent("argv"), encoding: .utf8).trimmingCharacters(in: .newlines)
    }

    func testProfileAddArguments() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":true,"revision":3}'"#))
        let reply = try await client.profileAdd(id: "lab", flag: "", name: "Lab", emoji: "🧪", configDir: "~/.claude-lab", makeDefault: true)
        XCTAssertEqual(reply.ok, true)
        XCTAssertEqual(reply.revision, 3)
        XCTAssertEqual(try argv(), "profile add --id lab --emoji 🧪 --config-dir ~/.claude-lab --name Lab --default --json")
    }

    func testProfileAddFailureKeepsIssues() async throws {
        let body = #"printf '{"ok":false,"error":"invalid config","issues":[{"path":"profiles[2].flag","message":"duplicate flag"}]}'; exit 1"#
        let client = CcsClient(executablePath: try script(body))
        let reply = try await client.profileAdd(id: "w", flag: "w", name: "", emoji: "x", configDir: "~/w", makeDefault: false)
        XCTAssertEqual(reply.ok, false)
        XCTAssertEqual(reply.failureText, "invalid config")
        XCTAssertEqual(reply.issues?.first?.path, "profiles[2].flag")
    }

    func testProfileRemoveWithNewDefault() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":true,"removed":"work"}'"#))
        _ = try await client.profileRemove(id: "work", newDefault: "personal")
        XCTAssertEqual(try argv(), "profile remove work --default personal --json")
    }

    func testConfigValidateDecodesOnExitOne() async throws {
        let json = String(decoding: try Fixtures.data("ccs/config_validate_invalid.json"), as: UTF8.self)
        let file = dir.appendingPathComponent("report.json")
        try json.write(to: file, atomically: true, encoding: .utf8)
        let client = CcsClient(executablePath: try script("cat \"\(file.path)\"; exit 1"))
        let report = try await client.configValidate()
        XCTAssertFalse(report.ok)
        XCTAssertEqual(report.issues.count, 4)
    }

    func testConfigDefaultsAsRawTree() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":true,"config":{"version":1},"profile":{"limits":{"session":{"pause":90}}}}'"#))
        let defaults = try await client.configDefaults()
        XCTAssertEqual(defaults["profile"]?.value(at: FieldPath.parse("limits.session.pause")), .int(90))
        XCTAssertEqual(try argv(), "config defaults --json")
    }

    func testStatuslineRevertConflict() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":false,"profile_id":"w","result":"conflict","hint":"edit by hand","restored":null}'; exit 1"#))
        let result = try await client.statuslineRevert(profile: "w")
        XCTAssertEqual(result.result, "conflict")
        XCTAssertEqual(result.hint, "edit by hand")
    }

    func testStatuslineApplyError() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":false,"error":"settings.json is not valid JSON","issues":[{"path":"invalid_settings","message":"x"}]}'; exit 1"#))
        let result = try await client.statuslineApply(profile: "w")
        XCTAssertEqual(result.ok, false)
        XCTAssertEqual(result.issues?.first?.path, "invalid_settings")
    }

    func testVersionIsPlainText() async throws {
        let client = CcsClient(executablePath: try script("echo 'ccs 0.1.0'"))
        let version = try await client.version()
        XCTAssertEqual(version, "ccs 0.1.0")
        XCTAssertEqual(try argv(), "--version")
    }

    func testCancellingTerminatesTheProcess() async throws {
        let client = CcsClient(executablePath: try script("sleep 30; printf '{}'"))
        let started = Date()
        let task = Task { try await client.authLogin(profile: "w") }
        try await Task.sleep(for: .milliseconds(300))
        task.cancel()
        do {
            _ = try await task.value
            XCTFail("expected cancellation")
        } catch is CancellationError {
            // expected
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 10, "the ccs process was terminated")
    }

    func testLogoutAndDaemonLogs() async throws {
        let client = CcsClient(executablePath: try script(#"printf '{"ok":true,"profile_id":"w","logged_in":false,"daemon_refreshed":true,"path":"/x/daemon.log","lines":["a","b"]}'"#))
        let logout = try await client.authLogout(profile: "w")
        XCTAssertEqual(logout.loggedIn, false)
        XCTAssertEqual(logout.daemonRefreshed, true)
        let logs = try await client.daemonLogs()
        XCTAssertEqual(logs.lines, ["a", "b"])
    }
}
