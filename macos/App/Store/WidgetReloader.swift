import Foundation
import WidgetKit

/// Pure throttle decision for widget reloads (ADR-0012): at most one reload per
/// 60 s, immediately when any profile's `status` or `supervisor.state` changes,
/// and one trailing reload for throttled changes.
struct WidgetReloadPolicy: Sendable {
    enum Decision: Equatable, Sendable {
        case reloadNow
        case scheduleTrailing(at: Date)
        case none
    }

    static let minInterval: TimeInterval = 60

    private(set) var lastReloadAt: Date?
    private(set) var lastSignature: [String: String]?
    private(set) var trailingPending = false

    /// The part of a snapshot whose change must reach widgets immediately.
    static func signature(_ snapshot: WidgetSnapshot) -> [String: String] {
        var sig: [String: String] = [:]
        for p in snapshot.profiles {
            sig[p.id] = "\(p.status.rawValue)|\(p.supervisor.state.rawValue)"
        }
        return sig
    }

    mutating func onSnapshot(_ snapshot: WidgetSnapshot, now: Date) -> Decision {
        let sig = Self.signature(snapshot)
        if sig != lastSignature {
            return reload(now: now, signature: sig)
        }
        if let last = lastReloadAt, now.timeIntervalSince(last) < Self.minInterval {
            if trailingPending { return .none }
            trailingPending = true
            return .scheduleTrailing(at: last.addingTimeInterval(Self.minInterval))
        }
        return reload(now: now, signature: sig)
    }

    /// The scheduled trailing reload fired.
    mutating func onTrailingFired(now: Date) -> Decision {
        guard trailingPending else { return .none }
        return reload(now: now, signature: lastSignature ?? [:])
    }

    private mutating func reload(now: Date, signature: [String: String]) -> Decision {
        lastReloadAt = now
        lastSignature = signature
        trailingPending = false
        return .reloadNow
    }
}

/// Applies `WidgetReloadPolicy` with `WidgetCenter`.
@MainActor
final class WidgetReloader {
    private var policy = WidgetReloadPolicy()
    private var trailingTask: Task<Void, Never>?
    private let reloadAll: @MainActor () -> Void

    init(reloadAll: @escaping @MainActor () -> Void = { WidgetCenter.shared.reloadAllTimelines() }) {
        self.reloadAll = reloadAll
    }

    func snapshotChanged(_ snapshot: WidgetSnapshot) {
        apply(policy.onSnapshot(snapshot, now: Date()))
    }

    private func apply(_ decision: WidgetReloadPolicy.Decision) {
        switch decision {
        case .reloadNow:
            trailingTask?.cancel()
            trailingTask = nil
            reloadAll()
        case .scheduleTrailing(let at):
            trailingTask?.cancel()
            let delay = max(0, at.timeIntervalSinceNow)
            trailingTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard !Task.isCancelled, let self else { return }
                self.apply(self.policy.onTrailingFired(now: Date()))
            }
        case .none:
            break
        }
    }
}
