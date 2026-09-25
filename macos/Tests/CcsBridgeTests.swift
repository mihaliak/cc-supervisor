@testable import CCSupervisor
import XCTest

final class CcsModelsTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ path: String) throws -> T {
        try CcsClient.decoder.decode(T.self, from: try Fixtures.data(path))
    }

    func testDaemonStatus() throws {
        let off = try decode(DaemonStatusResult.self, "ccs/daemon_status_not_installed.json")
        XCTAssertEqual(off.installed, false)
        XCTAssertEqual(DaemonBannerState.from(off), .notInstalled)

        let running = try decode(DaemonStatusResult.self, "ccs/daemon_status_running.json")
        XCTAssertEqual(running.responsive, true)
        XCTAssertEqual(running.uptimeS, 7980)
        XCTAssertEqual(running.daemonPid, 4242)
        XCTAssertEqual(DaemonBannerState.from(running), .hidden)

        var stopped = running
        stopped.responsive = false
        stopped.loaded = false
        XCTAssertEqual(DaemonBannerState.from(stopped), .stopped)
        stopped.loaded = true
        XCTAssertEqual(DaemonBannerState.from(stopped), .notResponding)
    }

    func testStatus() throws {
        let status = try decode(StatusResult.self, "ccs/status_offline.json")
        XCTAssertEqual(status.ok, true)
        XCTAssertEqual(status.daemon?.responsive, false)
        XCTAssertEqual(status.profiles?.map(\.id), ["personal", "work"])
        XCTAssertEqual(status.profiles?.first?.supervisor?.state, "normal")
    }

    func testWarmupAuthDoctorError() throws {
        let warmup = try decode(WarmupResult.self, "ccs/warmup.json")
        XCTAssertEqual(warmup.results?.map(\.decision), ["started", "skipped"])
        XCTAssertEqual(warmup.results?.last?.reason, "window_active")
        XCTAssertEqual(warmup.results?.first?.profileId, "work")

        let auth = try decode(AuthStatusResult.self, "ccs/auth_status.json")
        XCTAssertEqual(auth.loggedIn, true)
        XCTAssertEqual(auth.keychainService, "Claude Code-credentials-1e91dd84")

        let login = try decode(AuthLoginResult.self, "ccs/auth_login.json")
        XCTAssertEqual(login.mode, "headless")

        let doctor = try decode(DoctorResult.self, "ccs/doctor.json")
        XCTAssertEqual(doctor.summary?.fail, 1)
        XCTAssertEqual(doctor.checks?.count, 3)

        let error = try decode(CcsReply.self, "ccs/error.json")
        XCTAssertEqual(error.ok, false)
        XCTAssertEqual(error.error, "supervisor not running")
    }
}

final class CcsClientTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-client-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// A fake `ccs` that records its argv and prints `body`.
    private func script(_ body: String) throws -> String {
        let url = dir.appendingPathComponent("ccs")
        try ("#!/bin/sh\necho \"$@\" > \"\(dir.path)/argv\"\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url.path
    }

    func testAppendsJSONAndDecodes() async throws {
        let path = try script(#"printf '{"ok":true,"results":[{"profile_id":"work","decision":"started"}]}'"#)
        let result = try await CcsClient(executablePath: path).warmup(profile: "work")
        XCTAssertEqual(result.results?.first?.decision, "started")
        let argv = try String(contentsOf: dir.appendingPathComponent("argv"), encoding: .utf8)
        XCTAssertEqual(argv.trimmingCharacters(in: .whitespacesAndNewlines), "warmup --profile work --trigger manual --json")
    }

    func testFailureUsesJSONErrorThenStderr() async throws {
        let jsonError = try script(#"printf '{"ok":false,"error":"supervisor not running"}'; exit 1"#)
        do {
            _ = try await CcsClient(executablePath: jsonError).pause(profile: "work")
            XCTFail("expected failure")
        } catch let error as CcsError {
            XCTAssertEqual(error, .failed(exitCode: 1, message: "supervisor not running"))
        }

        let stderrOnly = try script("echo 'ccs: boom' >&2; exit 3")
        do {
            _ = try await CcsClient(executablePath: stderrOnly).status()
            XCTFail("expected failure")
        } catch let error as CcsError {
            XCTAssertEqual(error, .failed(exitCode: 3, message: "boom"))
        }
    }

    func testDecodeOnFailure() async throws {
        let path = try script(#"printf '{"checks":[],"summary":{"ok":0,"warn":0,"fail":2}}'; exit 1"#)
        let doctor = try await CcsClient(executablePath: path).doctor()
        XCTAssertEqual(doctor.summary?.fail, 2)
    }

    func testTimeout() async throws {
        let path = try script("sleep 30")
        let started = Date()
        do {
            _ = try await CcsClient(executablePath: path).run(["status"], as: CcsReply.self, timeout: .seconds(1))
            XCTFail("expected timeout")
        } catch let error as CcsError {
            XCTAssertEqual(error, .timeout(seconds: 1))
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)
    }

    func testNotFound() async {
        do {
            _ = try await CcsClient(executablePath: "/nonexistent/ccs").status()
            XCTFail("expected notFound")
        } catch let error as CcsError {
            XCTAssertEqual(error, .notFound(path: "/nonexistent/ccs"))
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testChildPathPrefixed() {
        let env = CcsClient.childEnvironment(base: ["PATH": "/usr/bin:/bin"], home: URL(fileURLWithPath: "/Users/u"))
        XCTAssertEqual(env["PATH"], "/Users/u/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    }
}

final class ConfigReaderTests: XCTestCase {
    private let home = URL(fileURLWithPath: "/Users/u")

    func testDefaultsAndOverrides() {
        let missing = ConfigReader.read(file: URL(fileURLWithPath: "/nonexistent/config.json"), home: home)
        XCTAssertEqual(missing.ccsPath, "/Users/u/.local/bin/ccs")
        XCTAssertEqual(missing.menuBarMode, .letterPercent)
        XCTAssertFalse(missing.fileExists)

        let custom = ConfigReader.parse(Data(#"{"ccs_path":"~/bin/ccs","display":{"menu_bar":"icon_only"}}"#.utf8), home: home)
        XCTAssertEqual(custom.ccsPath, "/Users/u/bin/ccs")
        XCTAssertEqual(custom.menuBarMode, .iconOnly)

        let nullPath = ConfigReader.parse(Data(#"{"ccs_path":null,"display":{"menu_bar":"weird"}}"#.utf8), home: home)
        XCTAssertEqual(nullPath.ccsPath, "/Users/u/.local/bin/ccs")
        XCTAssertEqual(nullPath.menuBarMode, .letterPercent)

        let legacy = ConfigReader.parse(Data(#"{"display":{"menu_bar":"emoji_percent"}}"#.utf8), home: home)
        XCTAssertEqual(legacy.menuBarMode, .letterPercent)
        XCTAssertEqual(MenuBarMode.normalized("emoji_percent"), "letter_percent")
        XCTAssertEqual(MenuBarMode.normalized("icon_only"), "icon_only")
    }

    func testStateLocationHonorsEnvironment() {
        XCTAssertEqual(
            StateLocation.stateDir(environment: ["CCS_STATE_DIR": "/tmp/x", "XDG_STATE_HOME": "/tmp/y"]).path,
            "/tmp/x"
        )
        XCTAssertEqual(StateLocation.stateDir(environment: ["XDG_STATE_HOME": "/tmp/y"]).path, "/tmp/y/ccs")
        XCTAssertTrue(StateLocation.stateDir(environment: [:]).path.hasSuffix("/.local/state/ccs"))
        XCTAssertEqual(StateLocation.configFile(environment: ["XDG_CONFIG_HOME": "/tmp/c"]).path, "/tmp/c/ccs/config.json")
        XCTAssertEqual(
            SnapshotLocation.sourceSnapshotFile(environment: ["CCS_STATE_DIR": "/tmp/x"]).path,
            "/tmp/x/widget/snapshot.json"
        )
        // Relative overrides are ignored, like ccs.paths.
        XCTAssertTrue(StateLocation.stateDir(environment: ["CCS_STATE_DIR": "rel"]).path.hasSuffix("/.local/state/ccs"))
    }
}
