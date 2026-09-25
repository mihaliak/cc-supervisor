@testable import CCSupervisor
import XCTest

final class LaunchAgentEnvironmentTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-agent-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// A plist shaped like `ccs.daemon.launchd.render_plist` writes it.
    private func plist(_ env: [String: String]?) throws -> URL {
        var doc: [String: Any] = [
            "Label": LaunchAgentEnvironment.label,
            "ProgramArguments": ["/Users/u/.local/bin/ccs", "daemon", "run"],
            "KeepAlive": true,
        ]
        if let env { doc["EnvironmentVariables"] = env }
        let url = dir.appendingPathComponent("\(LaunchAgentEnvironment.label).plist")
        try PropertyListSerialization.data(fromPropertyList: doc, format: .xml, options: 0).write(to: url)
        return url
    }

    func testPlistPathMatchesLaunchd() {
        XCTAssertEqual(
            LaunchAgentEnvironment.plistURL(home: URL(fileURLWithPath: "/Users/u")).path,
            "/Users/u/Library/LaunchAgents/local.ccsupervisor.daemon.plist"
        )
    }

    func testReadsOnlyThePathVariables() throws {
        let url = try plist([
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/u",
            "XDG_STATE_HOME": "/data/state",
            "XDG_CONFIG_HOME": "/data/config",
            "CCS_STATE_DIR": "/data/ccs-state",
        ])
        XCTAssertEqual(LaunchAgentEnvironment.pathVariables(plist: try Data(contentsOf: url)), [
            "XDG_STATE_HOME": "/data/state",
            "XDG_CONFIG_HOME": "/data/config",
            "CCS_STATE_DIR": "/data/ccs-state",
        ])
    }

    /// The daemon's values win, and one it doesn't have is dropped from the app's own env.
    func testAgentValuesReplaceTheAppsOwn() throws {
        let url = try plist(["CCS_STATE_DIR": "/data/ccs-state", "PATH": "/usr/bin"])
        let env = LaunchAgentEnvironment.resolve(
            base: ["PATH": "/app/bin", "XDG_CONFIG_HOME": "/elsewhere", "CCS_STATE_DIR": "/other", "LANG": "C"],
            plist: url
        )
        XCTAssertEqual(env, ["PATH": "/app/bin", "CCS_STATE_DIR": "/data/ccs-state", "LANG": "C"])

        XCTAssertEqual(StateLocation.stateDir(environment: env).path, "/data/ccs-state")
        XCTAssertEqual(StateLocation.daemonSocket(environment: env).path, "/data/ccs-state/daemon.sock")
        XCTAssertEqual(SnapshotLocation.sourceSnapshotFile(environment: env).path, "/data/ccs-state/widget/snapshot.json")
        XCTAssertEqual(
            StateLocation.configFile(environment: env).path,
            StateLocation.realHome().appendingPathComponent(".config/ccs/config.json").path
        )
    }

    /// `ccs.paths` precedence: `CCS_STATE_DIR`, else `$XDG_STATE_HOME/ccs`.
    func testXDGFromTheAgent() throws {
        let url = try plist(["XDG_STATE_HOME": "/data/state", "XDG_CONFIG_HOME": "/data/config"])
        let env = LaunchAgentEnvironment.resolve(base: [:], plist: url)
        XCTAssertEqual(StateLocation.stateDir(environment: env).path, "/data/state/ccs")
        XCTAssertEqual(StateLocation.configFile(environment: env).path, "/data/config/ccs/config.json")
    }

    func testPlistWithoutEnvironmentMeansDefaults() throws {
        let url = try plist(nil)
        XCTAssertEqual(LaunchAgentEnvironment.resolve(base: ["XDG_STATE_HOME": "/mine", "LANG": "C"], plist: url), ["LANG": "C"])
    }

    func testMissingOrBrokenPlistKeepsTheAppsEnvironment() throws {
        let base = ["XDG_STATE_HOME": "/mine", "PATH": "/bin"]
        XCTAssertEqual(LaunchAgentEnvironment.resolve(base: base, plist: dir.appendingPathComponent("missing.plist")), base)
        let broken = dir.appendingPathComponent("broken.plist")
        try Data("not a plist".utf8).write(to: broken)
        XCTAssertEqual(LaunchAgentEnvironment.resolve(base: base, plist: broken), base)
    }

    /// Every `ccs` the app runs gets the daemon's path variables.
    func testChildEnvironmentCarriesThem() throws {
        let url = try plist(["CCS_STATE_DIR": "/data/ccs-state"])
        let base = LaunchAgentEnvironment.resolve(base: ["PATH": "/usr/bin"], plist: url)
        let env = CcsClient.childEnvironment(base: base, home: URL(fileURLWithPath: "/Users/u"))
        XCTAssertEqual(env["CCS_STATE_DIR"], "/data/ccs-state")
        XCTAssertTrue(env["PATH"]?.hasPrefix("/Users/u/.local/bin:") == true)
    }
}

/// Widgets can only read `~/.local/state/ccs/widget/` (ADR-0012/0022). When the daemon's
/// state dir is elsewhere, the app warns instead of widgets silently staying "offline".
final class WidgetAccessWarningTests: XCTestCase {
    private let home = URL(fileURLWithPath: "/Users/u", isDirectory: true)

    private func warning(_ agentEnv: [String: String]) -> WidgetAccessWarning? {
        let env = LaunchAgentEnvironment.apply(agentEnv, to: ["XDG_STATE_HOME": "/inherited/by/the/app"])
        return WidgetAccessWarning.check(stateDir: StateLocation.stateDir(environment: env, home: home), home: home)
    }

    func testDefaultStateDirHasNoWarning() {
        XCTAssertNil(warning([:]))
        XCTAssertNil(warning(["XDG_STATE_HOME": "/Users/u/.local/state"]))
        XCTAssertNil(warning(["CCS_STATE_DIR": "/Users/u/.local/state/ccs/"]))
        XCTAssertNil(warning(["CCS_STATE_DIR": "/Users/u/.local/state/./ccs"]))
    }

    func testMovedStateDirWarns() throws {
        let moved = try XCTUnwrap(warning(["CCS_STATE_DIR": "/Users/u/data/ccs"]))
        XCTAssertTrue(moved.detail.contains("~/data/ccs"), moved.detail)
        XCTAssertTrue(moved.detail.contains("~/.local/state/ccs/widget/"))
        XCTAssertTrue(moved.detail.contains("Supervisor offline"))
        let xdg = try XCTUnwrap(warning(["XDG_STATE_HOME": "/Volumes/x/state"]))
        XCTAssertTrue(xdg.detail.contains("/Volumes/x/state/ccs"), xdg.detail)
    }

    func testWidgetPathMatchesTheEntitlement() throws {
        // `Widgets.entitlements` grants read access to exactly this home-relative folder.
        let widgetDir = StateLocation.defaultStateDir(home: home).appendingPathComponent("widget").path
        XCTAssertEqual(widgetDir, "/Users/u/.local/state/ccs/widget")
    }
}
