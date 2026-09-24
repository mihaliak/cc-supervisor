import AppKit
import Foundation
import Observation
import os

enum AppEnvironment {
    /// XCTest hosts the app; skip every side effect (daemon, notifications, ccs calls).
    static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }

    static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }
}

/// What the daemon-offline banner shows (from `ccs daemon status --json`).
enum DaemonBannerState: Equatable {
    case hidden
    case checking
    case ccsMissing(path: String)
    case notInstalled
    case stopped
    case notResponding
    case error(String)

    static func from(_ status: DaemonStatusResult) -> DaemonBannerState {
        if status.responsive == true { return .hidden }
        if status.installed != true { return .notInstalled }
        if status.loaded == true { return .notResponding }
        return .stopped
    }
}

struct CardMessage: Equatable {
    var text: String
    var isError: Bool
}

/// App state and actions. Every action is a `ccs` call (ADR-0001/0011).
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    let snapshotStore: SnapshotStore
    private(set) var config: AppConfig
    private(set) var daemonOnline = false
    private(set) var banner: DaemonBannerState = .checking
    private(set) var cardMessages: [String: CardMessage] = [:]
    private(set) var busy: Set<String> = []
    var selectedProfileID: String?
    /// Bumped to ask the always-rendered menu bar label to open Settings.
    private(set) var settingsRequest = 0
    private(set) var loginItemEnabled = false

    @ObservationIgnored private var ccs: CcsClient
    @ObservationIgnored private let daemon: DaemonConnection
    @ObservationIgnored private let reloader = WidgetReloader()
    @ObservationIgnored private var notifications: NotificationRouter?
    @ObservationIgnored private var triggers: OSTriggers?
    @ObservationIgnored private var didFireAppStart = false
    @ObservationIgnored private var started = false
    @ObservationIgnored private var messageTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private let log = Logger(subsystem: "local.ccsupervisor.app", category: "model")

    init(
        config: AppConfig = ConfigReader.read(),
        snapshotStore: SnapshotStore = SnapshotStore(),
        socketPath: String = StateLocation.daemonSocket().path
    ) {
        self.config = config
        self.snapshotStore = snapshotStore
        self.ccs = CcsClient(executablePath: config.ccsPath)
        self.daemon = DaemonConnection(socketPath: socketPath, appVersion: AppEnvironment.appVersion)
    }

    var snapshot: WidgetSnapshot? { snapshotStore.snapshot }

    // MARK: - Lifecycle

    func start() {
        guard !started, !AppEnvironment.isRunningTests else { return }
        started = true
        snapshotStore.onChange = { [weak self] snapshot in self?.reloader.snapshotChanged(snapshot) }
        snapshotStore.start()

        let router = NotificationRouter { [weak self] url in self?.open(url) }
        router.start()
        notifications = router

        let osTriggers = OSTriggers { [weak self] in self?.fireWarmupTrigger("unlock_wake") }
        osTriggers.start()
        triggers = osTriggers

        loginItemEnabled = LoginItemController.isEnabled
        daemon.start()
        Task { [weak self] in
            guard let stream = self?.daemon.updates else { return }
            for await update in stream { self?.handle(update) }
        }
        Task { await refreshBanner() }
    }

    private func handle(_ update: DaemonUpdate) {
        switch update {
        case .online(let online):
            daemonOnline = online
            log.notice("daemon \(online ? "online" : "offline", privacy: .public)")
            if online {
                banner = .hidden
                // ADR-0010 trigger 2: once per launch, when the daemon can act on it.
                if !didFireAppStart {
                    didFireAppStart = true
                    fireWarmupTrigger("app_start")
                }
            } else {
                Task { await refreshBanner() }
            }
        case .event(let event):
            notifications?.post(event)
        case .snapshot(let data):
            snapshotStore.apply(data: data)
        }
    }

    /// Forward an OS trigger; the daemon applies all skip rules (ADR-0010).
    private func fireWarmupTrigger(_ trigger: String) {
        guard daemonOnline else {
            log.info("skipping \(trigger, privacy: .public) warm-up trigger: daemon offline")
            return
        }
        let client = ccs
        Task {
            do {
                _ = try await client.warmupAll(trigger: trigger)
                log.notice("warm-up trigger \(trigger, privacy: .public) sent")
            } catch {
                log.error("warm-up trigger \(trigger, privacy: .public) failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// Re-read `config.json` (ccs path, menu bar mode). Called when the menu opens.
    func reloadConfig() {
        let fresh = ConfigReader.read()
        guard fresh != config else { return }
        if fresh.ccsPath != config.ccsPath { ccs = CcsClient(executablePath: fresh.ccsPath) }
        config = fresh
    }

    func menuOpened() {
        reloadConfig()
        snapshotStore.reload()
        loginItemEnabled = LoginItemController.isEnabled
        if !daemonOnline { Task { await refreshBanner() } }
    }

    func refreshBanner() async {
        guard !daemonOnline else {
            banner = .hidden
            return
        }
        guard FileManager.default.isExecutableFile(atPath: config.ccsPath) else {
            banner = .ccsMissing(path: config.ccsPath)
            return
        }
        do {
            let status = try await ccs.daemonStatus()
            if !daemonOnline { banner = DaemonBannerState.from(status) }
            if status.responsive == true { daemon.reconnectNow() }
        } catch {
            if !daemonOnline { banner = .error(error.localizedDescription) }
        }
    }

    // MARK: - Actions

    func refresh() {
        if daemonOnline {
            daemon.send(op: "refresh")
            return
        }
        perform(key: "refresh", profile: nil) { client in
            _ = try await client.usageRefresh()
            return nil
        }
    }

    func warmUp(profileID: String) {
        perform(key: "warmup:\(profileID)", profile: profileID) { client in
            let result = try await client.warmup(profile: profileID)
            let entry = result.results?.first { $0.profileId == profileID } ?? result.results?.first
            switch entry?.decision {
            case "started": return "Warm-up started"
            case "skipped": return "Warm-up skipped" + (entry?.reason.map { " (\($0))" } ?? "")
            default: return "Warm-up requested"
            }
        }
    }

    func togglePause(profile: ProfileSnapshot) {
        let paused = profile.supervisor.state == .paused
        perform(key: "pause:\(profile.id)", profile: profile.id) { client in
            if paused {
                _ = try await client.resume(profile: profile.id)
                return "Resumed"
            }
            _ = try await client.pause(profile: profile.id)
            return "Paused"
        }
    }

    func signIn(profileID: String) {
        perform(key: "signin:\(profileID)", profile: profileID, progress: "Waiting for sign-in in the browser…") { client in
            let result = try await client.authLogin(profile: profileID)
            if result.loggedIn == true {
                return "Signed in" + (result.account.map { " as \($0)" } ?? "")
            }
            throw CcsError.failed(exitCode: 1, message: result.error ?? "Sign-in did not complete")
        }
    }

    func installDaemon() {
        perform(key: "daemon", profile: nil) { client in
            _ = try await client.daemonInstall()
            return nil
        } after: { [weak self] in
            self?.daemon.reconnectNow()
            await self?.refreshBanner()
        }
    }

    func startDaemon() {
        perform(key: "daemon", profile: nil) { client in
            _ = try await client.daemonStart()
            return nil
        } after: { [weak self] in
            self?.daemon.reconnectNow()
            await self?.refreshBanner()
        }
    }

    func setLoginItem(_ enabled: Bool) {
        do {
            try LoginItemController.setEnabled(enabled)
        } catch {
            showMessage(key: "global", CardMessage(text: "Login item: \(error.localizedDescription)", isError: true))
        }
        loginItemEnabled = LoginItemController.isEnabled
    }

    // MARK: - Navigation

    func open(_ url: URL) {
        guard let link = DeepLink(url: url) else {
            log.info("ignoring unknown URL \(url.absoluteString, privacy: .public)")
            return
        }
        switch link {
        case .profile(let id): requestSettings(profileID: id)
        case .signIn(let id): signIn(profileID: id)
        case .refresh: refresh()
        }
    }

    func requestSettings(profileID: String? = nil) {
        if let profileID { selectedProfileID = profileID }
        settingsRequest += 1
    }

    // MARK: - Helpers

    func isBusy(_ key: String) -> Bool { busy.contains(key) }

    func message(for profileID: String) -> CardMessage? { cardMessages[profileID] }

    var globalMessage: CardMessage? { cardMessages["global"] }

    private func perform(
        key: String,
        profile: String?,
        progress: String? = nil,
        _ action: @escaping @Sendable (CcsClient) async throws -> String?,
        after: (@MainActor () async -> Void)? = nil
    ) {
        guard !busy.contains(key) else { return }
        busy.insert(key)
        let messageKey = profile ?? "global"
        if let progress { showMessage(key: messageKey, CardMessage(text: progress, isError: false), sticky: true) }
        let client = ccs
        Task {
            do {
                let text = try await action(client)
                if let text {
                    showMessage(key: messageKey, CardMessage(text: text, isError: false))
                } else if progress != nil {
                    clearMessage(key: messageKey)
                }
            } catch {
                showMessage(key: messageKey, CardMessage(text: error.localizedDescription, isError: true))
            }
            busy.remove(key)
            await after?()
        }
    }

    private func showMessage(key: String, _ message: CardMessage, sticky: Bool = false) {
        cardMessages[key] = message
        messageTasks[key]?.cancel()
        guard !sticky else { return }
        messageTasks[key] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(message.isError ? 12 : 6))
            guard !Task.isCancelled else { return }
            self?.clearMessage(key: key)
        }
    }

    private func clearMessage(key: String) {
        cardMessages[key] = nil
        messageTasks[key] = nil
    }
}
