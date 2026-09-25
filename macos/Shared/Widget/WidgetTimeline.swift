import Foundation

/// One precomputed timeline entry (P12): its date and the display at that date.
public struct WidgetTimelineItem: Equatable, Sendable {
    public var date: Date
    public var display: WidgetDisplay
}

/// Builds the per-minute entries so countdowns stay right between reloads
/// (ADR-0012). Relative strings are computed per entry date.
public enum WidgetTimeline {
    public static let entryCount = 60

    /// `now`, then the next `count - 1` whole-minute boundaries.
    public static func entryDates(now: Date, count: Int = entryCount) -> [Date] {
        guard count > 0 else { return [] }
        let nextMinute = (floor(now.timeIntervalSince1970 / 60) + 1) * 60
        return [now] + (0 ..< max(count - 1, 0)).map {
            Date(timeIntervalSince1970: nextMinute + Double($0) * 60)
        }
    }

    public static func items(
        snapshot: WidgetSnapshot?,
        profileID: String?,
        options: WidgetOptions = WidgetOptions(),
        now: Date,
        count: Int = entryCount,
        timeZone: TimeZone = .current
    ) -> [WidgetTimelineItem] {
        entryDates(now: now, count: count).map { date in
            WidgetTimelineItem(
                date: date,
                display: WidgetDisplayBuilder.build(
                    snapshot: snapshot, profileID: profileID, options: options,
                    now: date, loadedAt: now, timeZone: timeZone
                )
            )
        }
    }
}

/// Profiles offered by right-click → Edit Widget (read from the snapshot).
public struct WidgetProfileChoice: Equatable, Sendable, Identifiable {
    public var id: String
    public var name: String
    public var emoji: String

    public var title: String { ProfileTitle.text(emoji: emoji, name: name) }
}

public enum WidgetProfileCatalog {
    public static func choices(_ snapshot: WidgetSnapshot?) -> [WidgetProfileChoice] {
        (snapshot?.profiles ?? []).map { WidgetProfileChoice(id: $0.id, name: $0.name, emoji: $0.emoji) }
    }

    public static func choices(for ids: [String], in snapshot: WidgetSnapshot?) -> [WidgetProfileChoice] {
        let all = choices(snapshot)
        return ids.compactMap { id in all.first { $0.id == id } }
    }

    /// The first profile, used when a widget is added.
    public static func defaultChoice(_ snapshot: WidgetSnapshot?) -> WidgetProfileChoice? {
        choices(snapshot).first
    }
}

/// Built-in sample data for the widget gallery, placeholders, and previews.
public enum WidgetSamples {
    public static func snapshot(now: Date, paused: Bool = false, status: ProfileStatus = .ok) -> WidgetSnapshot {
        let session = now.addingTimeInterval(2 * 3600 + 13 * 60)
        let weekly = now.addingTimeInterval(2 * 86400 + 4 * 3600)
        let signedOut = status == .needsSignIn
        let rows: [UsageRow] = signedOut ? [] : [
            UsageRow(kind: .session, label: "Session", percent: paused ? 91 : 45,
                     level: paused ? .red : .green, resetsAt: session),
            UsageRow(kind: .weekly, label: "Weekly", percent: 62, level: .yellow, resetsAt: weekly),
            UsageRow(kind: .modelScoped, label: "Fable", percent: 4, level: .green, resetsAt: weekly),
            UsageRow(kind: .extraUsage, label: "Extra usage", percent: 32, level: .green,
                     resetsAt: nil, detail: "€3.20 / €10.00"),
        ]
        let supervisor = SupervisorSummary(
            state: paused ? .paused : .normal,
            activeSessions: 2,
            pausedSessions: paused ? 2 : 0,
            otherSessions: 1,
            resumeAt: paused ? session : nil,
            nextWarmupAt: now.addingTimeInterval(9 * 3600 + 12 * 60)
        )
        let profile = ProfileSnapshot(
            id: "work", name: "Work", emoji: "💼", status: status, stale: status == .stale,
            level: signedOut ? nil : (paused ? .red : .yellow),
            updatedAt: signedOut ? nil : now.addingTimeInterval(-60),
            rows: rows, supervisor: supervisor
        )
        return WidgetSnapshot(schema: 1, generatedAt: now.addingTimeInterval(-30), profiles: [profile])
    }

    public static let profileID = "work"
}
