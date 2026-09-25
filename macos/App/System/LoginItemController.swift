import Foundation
import ServiceManagement

/// Launch-at-login via `SMAppService.mainApp` (ADR-0011). Registration happens
/// only on explicit user action (menu/Settings toggle, or `make install` passing
/// `--register-login-item`); `make uninstall` passes `--unregister-login-item`.
@MainActor
enum LoginItemController {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    /// True when macOS needs something from the user (approval, or the app is not where it's registered).
    static var needsAttention: Bool {
        switch SMAppService.mainApp.status {
        case .requiresApproval, .notFound: true
        default: false
        }
    }

    static var statusDescription: String {
        switch SMAppService.mainApp.status {
        case .enabled: "enabled"
        case .requiresApproval: "needs approval in System Settings → General → Login Items"
        case .notRegistered: "off"
        case .notFound: "not found (run the app from ~/Applications)"
        @unknown default: "unknown"
        }
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }

    /// Handle `--register-login-item` / `--unregister-login-item` and exit.
    /// Returns without exiting when neither flag is present.
    static func handleLaunchArguments(_ arguments: [String] = CommandLine.arguments) {
        let register = arguments.contains("--register-login-item")
        let unregister = arguments.contains("--unregister-login-item")
        guard register || unregister else { return }
        do {
            try setEnabled(register)
            print("login item: \(statusDescription)")
            exit(0)
        } catch {
            FileHandle.standardError.write(Data("login item: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}
