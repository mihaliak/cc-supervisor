import WidgetKit

/// One timeline entry: the display precomputed for its date (P12).
struct UsageEntry: TimelineEntry {
    let date: Date
    let display: WidgetDisplay
    /// Small widget only: bar or gauge (Edit Widget).
    var smallStyle: SmallWidgetStyle = .bar

    init(date: Date, display: WidgetDisplay, smallStyle: SmallWidgetStyle = .bar) {
        self.date = date
        self.display = display
        self.smallStyle = smallStyle
    }

    init(_ item: WidgetTimelineItem, smallStyle: SmallWidgetStyle = .bar) {
        self.init(date: item.date, display: item.display, smallStyle: smallStyle)
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
