import WidgetKit

/// One timeline entry: the display precomputed for its date (P12).
struct UsageEntry: TimelineEntry {
    let date: Date
    let display: WidgetDisplay

    init(date: Date, display: WidgetDisplay) {
        self.date = date
        self.display = display
    }

    init(_ item: WidgetTimelineItem) {
        self.init(date: item.date, display: item.display)
    }

    /// Sample data for the gallery and placeholders.
    static func sample(now: Date, paused: Bool = false, status: ProfileStatus = .ok) -> UsageEntry {
        UsageEntry(
            date: now,
            display: WidgetDisplayBuilder.build(
                snapshot: WidgetSamples.snapshot(now: now, paused: paused, status: status),
                profileID: WidgetSamples.profileID,
                now: now
            )
        )
    }
}
