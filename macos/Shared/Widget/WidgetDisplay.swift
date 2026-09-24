import Foundation

/// Widget family, mirrored so the pure display logic doesn't depend on WidgetKit.
public enum WidgetSize: Sendable, CaseIterable {
    case small, medium, large

    /// Maximum usage rows the family shows (the small family shows only the session row).
    var rowCapacity: Int {
        switch self {
        case .small: 1
        case .medium: 4
        case .large: 6
        }
    }
}

/// Per-widget toggles from right-click → Edit Widget. Medium and large only.
public struct WidgetOptions: Sendable, Equatable {
    public var showWeekly: Bool
    public var showModelScoped: Bool
    public var showExtraUsage: Bool

    public init(showWeekly: Bool = true, showModelScoped: Bool = true, showExtraUsage: Bool = true) {
        self.showWeekly = showWeekly
        self.showModelScoped = showModelScoped
        self.showExtraUsage = showExtraUsage
    }
}

/// Entry-level state, evaluated per timeline entry date (P12 state table).
public enum WidgetState: Equatable, Sendable {
    /// No profile selected, or the selected id isn't in the snapshot.
    case notConfigured
    /// The snapshot is missing or `generated_at` was older than 5 min when the
    /// timeline was built (ADR-0005).
    case offline
    /// `status == needs_sign_in`.
    case needsSignIn
    /// `stale` flag / status, or `updated_at` older than 10 min.
    case stale
    /// The profile has no usage rows (no subscription, source error, nothing yet).
    case noData
    case ok
}

/// One usage row, ready to render.
public struct WidgetRowDisplay: Equatable, Sendable, Identifiable {
    public var id: String
    public var kind: RowKind
    public var label: String
    public var percent: Int
    public var percentText: String
    public var level: Level?
    /// `20:00 · in 2h 13m`, or the preformatted `detail` for extra usage.
    public var trailing: String?
}

/// The small widget's weekly footer: `W 50% · Sat 08:00`.
public struct WidgetCompactLine: Equatable, Sendable {
    public var prefix: String
    public var percentText: String
    public var level: Level?
    public var trailing: String?

    public var text: String {
        let head = "\(prefix) \(percentText)"
        guard let trailing, !trailing.isEmpty else { return head }
        return "\(head) · \(trailing)"
    }
}

/// Everything a widget view needs for one entry. Built from the Python-computed
/// snapshot; no threshold or level math happens here (ADR-0001).
public struct WidgetDisplay: Equatable, Sendable {
    public static let pausedBadge = "⏸ Paused"

    public var state: WidgetState
    public var profileID: String?
    public var emoji: String
    public var name: String
    public var isPaused: Bool
    /// `resumes 20:00 · in 42m` while paused with a known resume time.
    public var resumeText: String?
    public var session: WidgetRowDisplay?
    public var weeklyLine: WidgetCompactLine?
    /// Session first, then the rows enabled by `WidgetOptions`, in snapshot order.
    public var rows: [WidgetRowDisplay]
    /// `3 ccs sessions · 1 paused · 2 other`.
    public var supervisorLine: String?
    /// `Next warm-up 06:00 · in 9h 12m`.
    public var nextWarmupText: String?
    /// `Updated 1m ago`.
    public var updatedText: String?
    /// Main message for not configured / sign-in / no-data states.
    public var message: String?
    /// Footer for offline / stale states.
    public var footer: String?
    /// Values are shown at reduced opacity (offline, stale).
    public var dimmed: Bool
    /// Tap target for the whole widget.
    public var url: URL?

    public var title: String { emoji.isEmpty ? name : "\(emoji) \(name)" }

    /// Whether usage values are shown (dimmed when offline or stale) instead of a message.
    public var showsValues: Bool {
        guard session != nil || !rows.isEmpty else { return false }
        return state != .notConfigured && state != .needsSignIn
    }

    /// Rows for a family. When space runs out, drop extra usage, then model-scoped,
    /// then weekly (P12). The session row is never dropped.
    public func rows(for size: WidgetSize) -> [WidgetRowDisplay] {
        var out = rows
        if size == .small { return Array(out.filter { $0.kind == .session }.prefix(1)) }
        for kind in [RowKind.extraUsage, .modelScoped, .weekly] {
            while out.count > size.rowCapacity, let index = out.lastIndex(where: { $0.kind == kind }) {
                out.remove(at: index)
            }
        }
        return Array(out.prefix(size.rowCapacity))
    }
}

public enum WidgetDisplayBuilder {
    /// `updated_at` older than this marks the profile stale in the widget.
    public static let staleAfter: TimeInterval = 10 * 60

    public static func build(
        snapshot: WidgetSnapshot?,
        profileID: String?,
        options: WidgetOptions = WidgetOptions(),
        now: Date,
        loadedAt: Date? = nil,
        timeZone: TimeZone = .current
    ) -> WidgetDisplay {
        guard let profileID, !profileID.isEmpty else {
            return notConfigured()
        }
        guard let snapshot else {
            return WidgetDisplay(
                state: .offline, profileID: profileID, emoji: "", name: profileID,
                isPaused: false, resumeText: nil, session: nil, weeklyLine: nil, rows: [],
                supervisorLine: nil, nextWarmupText: nil, updatedText: nil,
                message: "No usage data yet", footer: "Supervisor offline", dimmed: true,
                url: DeepLink.profile(profileID).url
            )
        }
        guard let profile = snapshot.profile(id: profileID) else {
            return notConfigured()
        }

        let known = profile.rows.filter { $0.kind != .unknown }
        let allRows = known.map { row($0, now: now, timeZone: timeZone) }
        let session = allRows.first { $0.kind == .session }
        let visible = allRows.filter { r in
            switch r.kind {
            case .session: true
            case .weekly: options.showWeekly
            case .modelScoped: options.showModelScoped
            case .extraUsage: options.showExtraUsage
            case .unknown: false
            }
        }
        let ordered = visible.filter { $0.kind == .session } + visible.filter { $0.kind != .session }

        let isPaused = profile.supervisor.state == .paused
        let resumeText = isPaused ? profile.supervisor.resumeAt.map {
            "resumes " + TimeFormat.compact($0, now: now, timeZone: timeZone)
        } : nil
        let nextWarmup = profile.supervisor.nextWarmupAt.flatMap { at in
            at > now ? "Next warm-up " + TimeFormat.compact(at, now: now, timeZone: timeZone) : nil
        }
        let updatedAgo = profile.updatedAt.map { TimeFormat.ago($0, now: now) }

        let isStale = profile.stale || profile.status == .stale
            || (profile.updatedAt.map { now.timeIntervalSince($0) > staleAfter } ?? false)

        var message: String?
        if profile.status == .needsSignIn {
            message = "Sign in required"
        } else if known.isEmpty {
            message = noDataMessage(profile.status)
        }

        let state: WidgetState
        var footer: String?
        var dimmed = false
        // "Offline" is a claim about the daemon, so it's judged when the snapshot was
        // read (`loadedAt`), not at future entry dates: macOS may throttle widget
        // reloads, and later entries can't know whether newer data exists. Data age
        // is still judged per entry (stale).
        if snapshot.isDaemonOffline(now: loadedAt ?? now) {
            state = .offline
            footer = "Supervisor offline"
            dimmed = true
        } else if profile.status == .needsSignIn {
            state = .needsSignIn
        } else if isStale {
            state = .stale
            footer = updatedAgo.map { "updated \($0)" } ?? "not updated recently"
            dimmed = true
        } else if known.isEmpty {
            state = .noData
        } else {
            state = .ok
        }

        return WidgetDisplay(
            state: state,
            profileID: profile.id,
            emoji: profile.emoji,
            name: profile.name,
            isPaused: isPaused,
            resumeText: resumeText,
            session: session,
            weeklyLine: weeklyLine(profile, now: now, timeZone: timeZone),
            rows: ordered,
            supervisorLine: supervisorLine(profile.supervisor),
            nextWarmupText: nextWarmup,
            updatedText: updatedAgo.map { "Updated \($0)" },
            message: message,
            footer: footer,
            dimmed: dimmed,
            url: DeepLink.profile(profile.id).url
        )
    }

    static func notConfigured() -> WidgetDisplay {
        WidgetDisplay(
            state: .notConfigured, profileID: nil, emoji: "", name: "CC Supervisor",
            isPaused: false, resumeText: nil, session: nil, weeklyLine: nil, rows: [],
            supervisorLine: nil, nextWarmupText: nil, updatedText: nil,
            message: "Choose a profile", footer: "Right-click → Edit Widget", dimmed: false,
            url: nil
        )
    }

    static func row(_ r: UsageRow, now: Date, timeZone: TimeZone) -> WidgetRowDisplay {
        let trailing: String?
        if r.kind == .extraUsage, let detail = r.detail, !detail.isEmpty {
            trailing = detail
        } else if let resets = r.resetsAt {
            trailing = TimeFormat.compact(resets, now: now, timeZone: timeZone)
        } else {
            trailing = r.detail
        }
        return WidgetRowDisplay(
            id: r.id, kind: r.kind, label: r.label, percent: r.percent,
            percentText: "\(r.percent)%", level: r.level, trailing: trailing
        )
    }

    static func weeklyLine(_ profile: ProfileSnapshot, now: Date, timeZone: TimeZone) -> WidgetCompactLine? {
        guard let weekly = profile.rows.first(where: { $0.kind == .weekly }) else { return nil }
        return WidgetCompactLine(
            prefix: "W",
            percentText: "\(weekly.percent)%",
            level: weekly.level,
            trailing: weekly.resetsAt.map { TimeFormat.absolute($0, now: now, timeZone: timeZone) }
        )
    }

    /// `3 ccs sessions · 1 paused · 2 other` (paused omitted when zero).
    static func supervisorLine(_ s: SupervisorSummary) -> String {
        var parts = ["\(s.activeSessions) ccs \(s.activeSessions == 1 ? "session" : "sessions")"]
        if s.pausedSessions > 0 { parts.append("\(s.pausedSessions) paused") }
        parts.append("\(s.otherSessions) other")
        return parts.joined(separator: " · ")
    }

    static func noDataMessage(_ status: ProfileStatus) -> String {
        switch status {
        case .noSubscription: "No plan limits for this account"
        case .sourceError: "Usage unavailable"
        default: "No usage data yet"
        }
    }
}
