import Foundation

/// Where the Python side keeps its files (ADR-0005/0016), resolved the same way as
/// `ccs.paths`: `$CCS_STATE_DIR`, else `$XDG_STATE_HOME/ccs`, else `~/.local/state/ccs`.
/// The app passes the daemon LaunchAgent's environment (`LaunchAgentEnvironment`); the
/// widget uses its own, i.e. the default state dir (ADR-0022).
public enum StateLocation {
    /// The user's real home directory. Inside the widget sandbox `NSHomeDirectory()`
    /// is the container, so resolve it via `getpwuid` (ADR-0012 verification).
    public static func realHome() -> URL {
        #if SCREENSHOTS
        // The README screenshot harness runs with the demo environment's HOME.
        if let home = ProcessInfo.processInfo.environment["HOME"], home.hasPrefix("/") {
            return URL(fileURLWithPath: home, isDirectory: true)
        }
        #endif
        if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir {
            return URL(fileURLWithPath: String(cString: dir), isDirectory: true)
        }
        return URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
    }

    public static func stateDir(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = realHome()
    ) -> URL {
        if let override = environment["CCS_STATE_DIR"], override.hasPrefix("/") {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        if let xdg = environment["XDG_STATE_HOME"], xdg.hasPrefix("/") {
            return URL(fileURLWithPath: xdg, isDirectory: true).appendingPathComponent("ccs", isDirectory: true)
        }
        return defaultStateDir(home: home)
    }

    /// `~/.local/state/ccs`. The only state dir widgets can read: their sandbox exception is
    /// pinned to its `widget/` folder (`Widgets.entitlements`, ADR-0012/0022).
    public static func defaultStateDir(home: URL = realHome()) -> URL {
        home.appendingPathComponent(".local/state/ccs", isDirectory: true)
    }

    public static func configFile(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        let base: URL
        if let xdg = environment["XDG_CONFIG_HOME"], xdg.hasPrefix("/") {
            base = URL(fileURLWithPath: xdg, isDirectory: true)
        } else {
            base = realHome().appendingPathComponent(".config", isDirectory: true)
        }
        return base.appendingPathComponent("ccs/config.json")
    }

    public static func daemonSocket(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        stateDir(environment: environment).appendingPathComponent("daemon.sock")
    }
}

/// Where widgets and the app read `widget/snapshot.json` from (ADR-0012).
/// Primary: the state dir, readable by the sandboxed widget through its temporary
/// exception. Fallback A (App Group mirroring) is a build-setting switch:
/// define `CCS_WIDGET_APPGROUP` (and `CCS_APP_GROUP_ID` in Info.plist) to enable it.
public enum SnapshotLocation {
    public static let appGroupInfoKey = "CCSAppGroupID"

    public static func widgetDir(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        #if CCS_WIDGET_APPGROUP
        if let group = Bundle.main.object(forInfoDictionaryKey: appGroupInfoKey) as? String,
           let container = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) {
            return container.appendingPathComponent("widget", isDirectory: true)
        }
        #endif
        return StateLocation.stateDir(environment: environment).appendingPathComponent("widget", isDirectory: true)
    }

    public static func snapshotFile(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        widgetDir(environment: environment).appendingPathComponent("snapshot.json")
    }

    /// The daemon-written source file (always the state dir; the app mirrors it
    /// into the App Group only when fallback A is enabled).
    public static func sourceSnapshotFile(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        StateLocation.stateDir(environment: environment).appendingPathComponent("widget/snapshot.json")
    }
}
