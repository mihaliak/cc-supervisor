import Foundation

/// Widgets read `widget/snapshot.json` only from the default state dir: their sandbox
/// exception is pinned to `~/.local/state/ccs/widget/` (ADR-0012/0022). When the daemon's
/// LaunchAgent moves the state dir (`CCS_STATE_DIR`, `XDG_STATE_HOME`), the app still works
/// but widgets stay "Supervisor offline". This says why, in the menu and in Settings.
struct WidgetAccessWarning: Equatable {
    var title: String
    var detail: String

    /// The warning for a daemon state dir, or nil when widgets can read it.
    static func check(stateDir: URL, home: URL) -> WidgetAccessWarning? {
        #if CCS_WIDGET_APPGROUP
        return nil  // fallback A: the app mirrors the snapshot into the App Group
        #else
        let readable = StateLocation.defaultStateDir(home: home)
        guard stateDir.standardizedFileURL.path != readable.standardizedFileURL.path else { return nil }
        return WidgetAccessWarning(
            title: "Widgets can't read the supervisor's data",
            detail: "The daemon keeps its state in \(abbreviate(stateDir, home: home)). "
                + "Widgets can only read ~/.local/state/ccs/widget/, so they show “Supervisor offline”. "
                + "To use widgets, run “ccs daemon install” without CCS_STATE_DIR or XDG_STATE_HOME set."
        )
        #endif
    }

    /// For the installed daemon's environment (`LaunchAgentEnvironment`).
    static var current: WidgetAccessWarning? {
        #if SCREENSHOTS
        return nil  // the demo environment moves the state dir on purpose
        #else
        return check(stateDir: LaunchAgentEnvironment.stateDir, home: StateLocation.realHome())
        #endif
    }

    private static func abbreviate(_ url: URL, home: URL) -> String {
        let path = url.standardizedFileURL.path
        let homePath = home.standardizedFileURL.path
        guard path.hasPrefix(homePath + "/") else { return path }
        return "~" + path.dropFirst(homePath.count)
    }
}
