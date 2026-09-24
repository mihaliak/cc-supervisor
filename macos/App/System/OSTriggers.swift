import AppKit
import Foundation

/// At most one trigger per window (ADR-0010: unlock/wake debounced to 60 s).
struct TriggerDebouncer: Sendable {
    let window: TimeInterval
    private(set) var lastFired: Date?

    init(window: TimeInterval = 60) {
        self.window = window
    }

    mutating func shouldFire(now: Date) -> Bool {
        if let lastFired, now.timeIntervalSince(lastFired) < window { return false }
        lastFired = now
        return true
    }
}

/// Forwards screen unlock and wake to the daemon as `unlock_wake` warm-up
/// triggers (ADR-0010). The daemon applies every skip rule; this only observes.
@MainActor
final class OSTriggers {
    private var debouncer = TriggerDebouncer()
    private var tokens: [(NotificationCenter, NSObjectProtocol)] = []
    private let onUnlockOrWake: @MainActor () -> Void

    init(onUnlockOrWake: @escaping @MainActor () -> Void) {
        self.onUnlockOrWake = onUnlockOrWake
    }

    func start() {
        guard tokens.isEmpty else { return }
        let distributed = DistributedNotificationCenter.default()
        let workspace = NSWorkspace.shared.notificationCenter
        tokens.append((distributed, distributed.addObserver(
            forName: Notification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.fire() }
        }))
        tokens.append((workspace, workspace.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.fire() }
        }))
    }

    func stop() {
        for (center, token) in tokens { center.removeObserver(token) }
        tokens.removeAll()
    }

    private func fire() {
        guard debouncer.shouldFire(now: Date()) else { return }
        onUnlockOrWake()
    }
}
