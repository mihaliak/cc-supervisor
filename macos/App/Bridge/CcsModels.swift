import Foundation

// Result models for `ccs … --json` (ADR-0017). Decoded with
// `.convertFromSnakeCase`; every field optional so contract growth never breaks
// the app. Fixtures: `schema/fixtures/ccs/`.

/// Any `ccs` JSON reply: `{"ok": bool, "error": "…", "issues": […]}`.
struct CcsReply: Decodable, Sendable {
    var ok: Bool?
    var error: String?
    var issues: [CcsIssue]?
    var revision: Int?

    /// `error`, else the issue messages joined, else a fallback.
    var failureText: String {
        if let error, !error.isEmpty { return error }
        let parts = (issues ?? []).compactMap { issue -> String? in
            guard let message = issue.message else { return nil }
            guard let path = issue.path, !path.isEmpty else { return message }
            return "\(path): \(message)"
        }
        return parts.isEmpty ? "ccs reported an error" : parts.joined(separator: "; ")
    }
}

struct CcsIssue: Decodable, Sendable, Equatable {
    var path: String?
    var message: String?
}

/// `ccs daemon status --json`.
struct DaemonStatusResult: Decodable, Sendable, Equatable {
    var ok: Bool?
    var installed: Bool?
    var loaded: Bool?
    var state: String?
    var pid: Int?
    var responsive: Bool?
    var version: String?
    var uptimeS: Int?
    var latencyMs: Double?
    var daemonPid: Int?
}

/// `ccs status --json` (subset the app uses).
struct StatusResult: Decodable, Sendable {
    struct Daemon: Decodable, Sendable {
        var responsive: Bool?
        var pid: Int?
        var version: String?
        var uptimeS: Int?
    }

    struct Profile: Decodable, Sendable {
        struct Supervisor: Decodable, Sendable {
            var state: String?
        }

        var id: String
        var supervisor: Supervisor?
        var nextWarmupAt: String?
    }

    var ok: Bool?
    var daemon: Daemon?
    var profiles: [Profile]?
}

/// `ccs warmup … --json` (P08): `{"results":[{"profile_id","decision","reason"}]}`.
struct WarmupResult: Decodable, Sendable {
    struct Entry: Decodable, Sendable, Equatable {
        var profileId: String?
        var decision: String?
        var reason: String?
    }

    var ok: Bool?
    var error: String?
    var results: [Entry]?
}

/// `ccs auth status --profile <id> --json` (P09).
struct AuthStatusResult: Decodable, Sendable, Equatable {
    var profileId: String?
    var configDir: String?
    var loggedIn: Bool?
    var account: String?
    var subscriptionType: String?
    var authMethod: String?
    var keychainService: String?
    var error: String?
}

/// `ccs auth logout --profile <id> --json` (P09).
struct AuthLogoutResult: Decodable, Sendable {
    var ok: Bool?
    var profileId: String?
    var loggedIn: Bool?
    var daemonRefreshed: Bool?
    var error: String?
}

/// `ccs daemon logs --json` (P04): the last lines of the daemon log.
struct DaemonLogsResult: Decodable, Sendable {
    var ok: Bool?
    var path: String?
    var lines: [String]?
}

/// `ccs statusline preview --profile <id> --json` (P07).
struct StatuslinePreviewResult: Decodable, Sendable, Equatable {
    struct Segment: Decodable, Sendable, Equatable {
        var text: String
        /// `green | yellow | red | gray | plain`
        var color: String?
    }

    struct Sample: Decodable, Sendable, Equatable, Identifiable {
        var state: String
        var plain: String?
        var segments: [Segment]?

        var id: String { state }
    }

    struct Status: Decodable, Sendable, Equatable {
        var applied: Bool?
        var scriptPath: String?
        var scriptCurrent: Bool?
    }

    var ok: Bool?
    var profileId: String?
    var status: Status?
    var samples: [Sample]?
}

/// `ccs statusline apply --profile <id> --json` (P07). `result`: `applied | already_applied`;
/// errors carry `issues[0].path` = `invalid_settings | statusline_disabled`.
struct StatuslineApplyResult: Decodable, Sendable {
    var ok: Bool?
    var profileId: String?
    var settingsPath: String?
    var result: String?
    var backupPath: String?
    var command: String?
    var error: String?
    var issues: [CcsIssue]?
}

/// `ccs statusline revert --profile <id> --json` (P07). `result`: `reverted | not_applied |
/// conflict` (conflict exits 1 and carries a `hint`).
struct StatuslineRevertResult: Decodable, Sendable {
    var ok: Bool?
    var profileId: String?
    var result: String?
    var hint: String?
    var error: String?
    var issues: [CcsIssue]?
}

/// `ccs auth login --profile <id> --json` (P09).
struct AuthLoginResult: Decodable, Sendable {
    var profileId: String?
    var mode: String?
    var loggedIn: Bool?
    var account: String?
    var error: String?
}

/// `ccs doctor --json` (P13): `{"checks":[…],"summary":{"ok","warn","fail"}}`.
struct DoctorResult: Decodable, Sendable {
    struct Check: Decodable, Sendable {
        var id: String?
        var scope: String?
        var status: String?
        var message: String?
        var fix: String?
    }

    struct Summary: Decodable, Sendable {
        var ok: Int?
        var warn: Int?
        var fail: Int?
    }

    var checks: [Check]?
    var summary: Summary?
}

enum CcsError: Error, Equatable, LocalizedError {
    case notFound(path: String)
    case launch(String)
    case timeout(seconds: Int)
    case failed(exitCode: Int32, message: String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notFound(let path): "ccs not found at \(path)"
        case .launch(let why): "could not run ccs: \(why)"
        case .timeout(let seconds): "ccs timed out after \(seconds) s"
        case .failed(_, let message): message
        case .decoding(let why): "unexpected ccs output: \(why)"
        }
    }
}
