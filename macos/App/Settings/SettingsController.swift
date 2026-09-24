import AppKit
import Foundation
import Observation
import os

/// A result line under a button in Settings.
struct SettingsMessage: Equatable, Sendable {
    var text: String
    var isError: Bool

    static func info(_ text: String) -> SettingsMessage { SettingsMessage(text: text, isError: false) }
    static func error(_ text: String) -> SettingsMessage { SettingsMessage(text: text, isError: true) }
}

/// Warm-up info shown read-only in the profile editor.
struct WarmupInfo: Equatable, Sendable {
    var nextAt: Date?
    var lastAttemptAt: Date?
    var lastResult: String?
    var lastReason: String?
}

enum SettingsTab: Hashable {
    case general
    case profiles
}

/// State and actions behind the Settings window (P11). Config edits go through
/// `ConfigStore`; everything else is a `ccs … --json` call (ADR-0001/0011).
@MainActor
@Observable
final class SettingsController {
    let app: AppModel
    let store: ConfigStore

    var selectedTab: SettingsTab = .general

    // General
    private(set) var ccsVersion: String?
    private(set) var ccsVersionError: String?
    private(set) var daemonStatus: DaemonStatusResult?
    private(set) var daemonError: String?
    private(set) var doctor: DoctorResult?
    private(set) var doctorError: String?
    private(set) var logs: DaemonLogsResult?
    var showingLogs = false

    // Profiles
    private(set) var authStatus: [String: AuthStatusResult] = [:]
    private(set) var authErrors: [String: String] = [:]
    private(set) var preview: [String: StatuslinePreviewResult] = [:]
    private(set) var warmupInfo: [String: WarmupInfo] = [:]
    private(set) var messages: [String: SettingsMessage] = [:]
    private(set) var busy: Set<String> = []

    @ObservationIgnored private var signInTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var watchTask: Task<Void, Never>?
    @ObservationIgnored private var lastHandledRequest = 0
    @ObservationIgnored private let log = Logger(subsystem: "local.ccsupervisor.app", category: "settings")

    init(app: AppModel, store: ConfigStore = ConfigStore()) {
        self.app = app
        self.store = store
        store.validator = { [weak self] in
            guard let self else { return .unavailable("Settings closed") }
            return await self.runValidation()
        }
        store.onSaved = { [weak app] in app?.reloadConfig() }
    }

    /// The client for the current `ccs_path` (it can change while Settings is open).
    private var ccs: CcsClient {
        CcsClient(executablePath: app.config.ccsPath)
    }

    // MARK: - Window lifecycle

    func windowAppeared() {
        store.reloadIfChanged()
        handleSettingsRequest()
        if app.selectedProfileID == nil || !store.profiles.contains(where: { $0.id == app.selectedProfileID }) {
            app.selectedProfileID = store.defaultProfileID ?? store.profiles.first?.id
        }
        Task {
            await loadDefaults()
            await store.validate()
        }
        refreshGeneral()
        startWatching()
    }

    func windowDisappeared() {
        watchTask?.cancel()
        watchTask = nil
        Task { await store.saveNow() }
    }

    /// A deep link (`ccsupervisor://profile/<id>`) or a card's ⚙ asked for a profile:
    /// show the Profiles tab with it selected.
    func handleSettingsRequest() {
        guard app.settingsRequest != lastHandledRequest else { return }
        lastHandledRequest = app.settingsRequest
        if app.settingsRequestProfileID != nil { selectedTab = .profiles }
    }

    /// Pick up edits made by the CLI or daemon while the window is open.
    private func startWatching() {
        watchTask?.cancel()
        watchTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self, !Task.isCancelled else { return }
                if self.store.reloadIfChanged() {
                    await self.store.validate()
                }
            }
        }
    }

    private func loadDefaults() async {
        guard store.defaults == nil else { return }
        do {
            store.setDefaults(try await ccs.configDefaults())
        } catch {
            log.error("config defaults: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func runValidation() async -> ValidationOutcome {
        do {
            return .report(try await ccs.configValidate())
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }

    /// `ccs profile list` seeds `config.json` when it doesn't exist yet (P02).
    func createConfig() {
        perform("create-config", key: "general") { client in
            _ = try await client.profileList()
            return nil
        } after: { [weak self] in
            self?.store.load()
            await self?.store.validate()
            self?.app.reloadConfig()
        }
    }

    // MARK: - General

    func refreshGeneral() {
        let client = ccs
        Task {
            do {
                ccsVersion = try await client.version()
                ccsVersionError = nil
            } catch {
                ccsVersion = nil
                ccsVersionError = error.localizedDescription
            }
        }
        refreshDaemonStatus()
    }

    func refreshDaemonStatus() {
        let client = ccs
        Task {
            do {
                daemonStatus = try await client.daemonStatus()
                daemonError = nil
            } catch {
                daemonStatus = nil
                daemonError = error.localizedDescription
            }
        }
    }

    enum DaemonAction: String {
        case install, start, stop, restart
    }

    func daemon(_ action: DaemonAction) {
        perform("daemon", key: "daemon") { client in
            switch action {
            case .install: _ = try await client.daemonInstall()
            case .start: _ = try await client.daemonStart()
            case .stop: _ = try await client.daemonStop()
            case .restart: _ = try await client.daemonRestart()
            }
            return "Daemon: \(action.rawValue) done"
        } after: { [weak self] in
            self?.refreshDaemonStatus()
            await self?.app.refreshBanner()
        }
    }

    func showLogs() {
        perform("logs", key: "daemon") { [weak self] client in
            let result = try await client.daemonLogs()
            await MainActor.run {
                self?.logs = result
                self?.showingLogs = true
            }
            return nil
        }
    }

    func runDoctor() {
        perform("doctor", key: "doctor") { [weak self] client in
            do {
                let result = try await client.doctor()
                await MainActor.run {
                    self?.doctor = result
                    self?.doctorError = nil
                }
            } catch {
                await MainActor.run {
                    self?.doctor = nil
                    self?.doctorError = "Diagnostics unavailable: \(error.localizedDescription)"
                }
            }
            return nil
        }
    }

    func browseForCcs() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.showsHiddenFiles = true
        panel.directoryURL = StateLocation.realHome().appendingPathComponent(".local/bin")
        panel.prompt = "Use"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        store.set(.root("ccs_path"), .string(DirectoryField.abbreviate(url.path)))
    }

    // MARK: - Profiles

    func profileAppeared(_ id: String) {
        refreshAuth(id)
        refreshPreview(id)
        refreshWarmupInfo(id)
    }

    func refreshAuth(_ id: String) {
        let client = ccs
        Task {
            do {
                authStatus[id] = try await client.authStatus(profile: id)
                authErrors[id] = nil
            } catch {
                authStatus[id] = nil
                authErrors[id] = error.localizedDescription
            }
        }
    }

    func signIn(_ id: String) {
        guard signInTasks[id] == nil else { return }
        let key = "signin:\(id)"
        busy.insert(key)
        messages[id] = .info("Complete sign-in in your browser or the Terminal window…")
        let client = ccs
        signInTasks[id] = Task { [weak self] in
            let message: SettingsMessage
            do {
                let result = try await client.authLogin(profile: id)
                if result.loggedIn == true {
                    message = .info("Signed in" + (result.account.map { " as \($0)" } ?? ""))
                } else {
                    message = .error(result.error ?? "Sign-in did not complete")
                }
            } catch is CancellationError {
                message = .info("Sign-in cancelled")
            } catch {
                message = .error(error.localizedDescription)
            }
            guard let self else { return }
            self.messages[id] = message
            self.busy.remove(key)
            self.signInTasks[id] = nil
            self.refreshAuth(id)
            self.app.refresh()
        }
    }

    func cancelSignIn(_ id: String) {
        signInTasks[id]?.cancel()
    }

    func isSigningIn(_ id: String) -> Bool { signInTasks[id] != nil }

    /// Call only after the user confirmed (P09: `--json` never prompts).
    func signOut(_ id: String) {
        perform("signout:\(id)", key: id) { client in
            let result = try await client.authLogout(profile: id)
            if result.ok == false || result.loggedIn == true {
                throw CcsError.failed(exitCode: 1, message: result.error ?? "Sign-out failed")
            }
            return "Signed out"
        } after: { [weak self] in
            self?.refreshAuth(id)
        }
    }

    func refreshPreview(_ id: String) {
        let client = ccs
        Task {
            do {
                preview[id] = try await client.statuslinePreview(profile: id)
            } catch {
                preview[id] = nil
                messages["statusline:\(id)"] = .error("Preview unavailable: \(error.localizedDescription)")
            }
        }
    }

    func applyStatusline(_ id: String) {
        perform("statusline:\(id)", key: "statusline:\(id)") { client in
            let result = try await client.statuslineApply(profile: id)
            guard result.ok != false else {
                throw CcsError.failed(exitCode: 1, message: result.error ?? result.issues?.first?.message ?? "Apply failed")
            }
            if result.result == "already_applied" { return "Already applied" }
            return "Applied" + (result.backupPath.map { ". Backup: \($0)" } ?? "")
        } after: { [weak self] in
            self?.refreshPreview(id)
        }
    }

    func revertStatusline(_ id: String) {
        perform("statusline:\(id)", key: "statusline:\(id)") { client in
            let result = try await client.statuslineRevert(profile: id)
            switch result.result {
            case "reverted": return "Reverted: the previous statusLine is restored"
            case "not_applied": return "Nothing to revert: the statusline wasn't applied"
            case "conflict": throw CcsError.failed(exitCode: 1, message: result.hint ?? "settings.json changed since apply; revert by hand")
            default:
                if result.ok == false {
                    throw CcsError.failed(exitCode: 1, message: result.error ?? result.issues?.first?.message ?? "Revert failed")
                }
                return result.result ?? "Done"
            }
        } after: { [weak self] in
            self?.refreshPreview(id)
        }
    }

    func refreshWarmupInfo(_ id: String) {
        let client = ccs
        Task {
            var info = WarmupInfo()
            if let status = try? await client.status(profile: id),
               let entry = status.profiles?.first(where: { $0.id == id }) {
                info.nextAt = entry.nextWarmupAt.flatMap(Self.parseDate)
            }
            let file = StateLocation.stateDir().appendingPathComponent("warmup/\(id).json")
            if let data = try? Data(contentsOf: file), let doc = try? JSONValue.parse(data),
               let last = doc["last_attempt"] {
                info.lastAttemptAt = last["at"]?.stringValue.flatMap(Self.parseDate)
                info.lastResult = last["result"]?.stringValue
                info.lastReason = last["reason"]?.stringValue
            }
            warmupInfo[id] = info
        }
    }

    static func parseDate(_ text: String) -> Date? {
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = full.date(from: text) { return date }
        return ISO8601DateFormatter().date(from: text)
    }

    struct NewProfile: Equatable {
        var id = ""
        var flag = ""
        var name = ""
        var emoji = ""
        var configDir = ""
        var makeDefault = false
    }

    /// `ccs profile add … --json`: Python fills every default (ADR-0004). Returns an error
    /// message, or nil on success.
    func addProfile(_ new: NewProfile) async -> String? {
        await store.saveNow()
        do {
            let reply = try await ccs.profileAdd(
                id: new.id.trimmingCharacters(in: .whitespaces),
                flag: new.flag.trimmingCharacters(in: .whitespaces),
                name: new.name.trimmingCharacters(in: .whitespaces),
                emoji: new.emoji.trimmingCharacters(in: .whitespaces),
                configDir: new.configDir.trimmingCharacters(in: .whitespaces),
                makeDefault: new.makeDefault
            )
            guard reply.ok == true else { return reply.failureText }
        } catch {
            return error.localizedDescription
        }
        store.load()
        await store.validate()
        app.selectedProfileID = new.id
        app.refresh()
        return nil
    }

    /// `ccs profile remove <id> --json` (the Claude config dir is never touched).
    func removeProfile(_ id: String) {
        let others = store.profiles.map(\.id).filter { $0 != id }
        let newDefault = store.defaultProfileID == id ? others.first : nil
        perform("remove:\(id)", key: "general") { [weak self] client in
            await self?.store.saveNow()
            let reply = try await client.profileRemove(id: id, newDefault: newDefault)
            guard reply.ok == true else { throw CcsError.failed(exitCode: 1, message: reply.failureText) }
            return "Removed profile \(id)"
        } after: { [weak self] in
            guard let self else { return }
            self.store.load()
            await self.store.validate()
            if self.app.selectedProfileID == id {
                self.app.selectedProfileID = self.store.defaultProfileID ?? self.store.profiles.first?.id
            }
            self.app.refresh()
        }
    }

    // MARK: - Helpers

    func isBusy(_ action: String) -> Bool { busy.contains(action) }

    func message(_ key: String) -> SettingsMessage? { messages[key] }

    private func perform(
        _ action: String,
        key: String,
        _ body: @escaping @Sendable (CcsClient) async throws -> String?,
        after: (@MainActor () async -> Void)? = nil
    ) {
        guard !busy.contains(action) else { return }
        busy.insert(action)
        let client = ccs
        Task {
            do {
                if let text = try await body(client) { messages[key] = .info(text) }
            } catch {
                messages[key] = .error(error.localizedDescription)
            }
            busy.remove(action)
            await after?()
        }
    }
}
