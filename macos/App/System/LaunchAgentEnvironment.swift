import Foundation

/// The environment the daemon runs with, from the installed LaunchAgent (ADR-0022).
///
/// `ccs daemon install` records `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and `CCS_STATE_DIR` in the
/// plist's `EnvironmentVariables`. The app resolves its socket, snapshot and config paths
/// from them (same precedence as `ccs.paths`, via `StateLocation`) and passes them to every
/// `ccs` it runs, so the app, the CLI and the daemon agree on one state dir and config file.
/// A missing or unreadable plist leaves the app's own environment as is. The widget always
/// reads the default state dir (its sandbox exception, ADR-0012).
enum LaunchAgentEnvironment {
    static let label = "local.ccsupervisor.daemon"

    /// The variables that move `ccs` paths (`ccs.paths`).
    static let pathKeys = ["CCS_STATE_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME"]

    static func plistURL(home: URL = StateLocation.realHome()) -> URL {
        home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    /// The path variables in a LaunchAgent plist, or nil when it isn't a readable plist.
    /// A plist without `EnvironmentVariables` yields `[:]` (the daemon has none of them).
    static func pathVariables(plist data: Data) -> [String: String]? {
        guard let doc = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any] else {
            return nil
        }
        let env = doc["EnvironmentVariables"] as? [String: Any] ?? [:]
        var out: [String: String] = [:]
        for key in pathKeys {
            if let value = env[key] as? String { out[key] = value }
        }
        return out
    }

    /// `base` with its path variables replaced by the agent's: a variable the daemon doesn't
    /// have is removed, so a value inherited by the app can't point it somewhere else.
    static func apply(_ agent: [String: String]?, to base: [String: String]) -> [String: String] {
        guard let agent else { return base }
        var env = base
        for key in pathKeys { env[key] = agent[key] }
        return env
    }

    static func resolve(base: [String: String], plist: URL) -> [String: String] {
        apply((try? Data(contentsOf: plist)).flatMap { pathVariables(plist: $0) }, to: base)
    }

    /// Read once at launch. Tests never read the real LaunchAgent.
    static let resolved: [String: String] = AppEnvironment.isRunningTests
        ? ProcessInfo.processInfo.environment
        : resolve(base: ProcessInfo.processInfo.environment, plist: plistURL())

    static var stateDir: URL { StateLocation.stateDir(environment: resolved) }
    static var configFile: URL { StateLocation.configFile(environment: resolved) }
    static var daemonSocket: URL { StateLocation.daemonSocket(environment: resolved) }
    static var sourceSnapshotFile: URL { SnapshotLocation.sourceSnapshotFile(environment: resolved) }
}
