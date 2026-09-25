import WidgetKit

/// Loads the snapshot once and emits one entry per minute for the next hour, so
/// countdowns stay current between (throttled) reloads (ADR-0012).
struct UsageTimelineProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> UsageEntry {
        UsageEntry.sample(now: Date())
    }

    func snapshot(for configuration: ProfileWidgetIntent, in context: Context) async -> UsageEntry {
        let now = Date()
        let snapshot = try? SnapshotLoader.load().get()
        let profileID = configuration.profileID
        // Widget gallery without real data for this profile: show the sample.
        if context.isPreview, snapshot?.profile(id: profileID ?? "") == nil {
            var sample = UsageEntry.sample(now: now)
            sample.smallStyle = configuration.style
            return sample
        }
        return UsageEntry(
            date: now,
            display: WidgetDisplayBuilder.build(
                snapshot: snapshot, profileID: profileID, options: configuration.options, now: now
            ),
            smallStyle: configuration.style
        )
    }

    func timeline(for configuration: ProfileWidgetIntent, in context: Context) async -> Timeline<UsageEntry> {
        let now = Date()
        let snapshot = try? SnapshotLoader.load().get()
        let entries = WidgetTimeline.items(
            snapshot: snapshot,
            profileID: configuration.profileID,
            options: configuration.options,
            now: now
        ).map { UsageEntry($0, smallStyle: configuration.style) }
        let reloadAt = entries.last?.date ?? now.addingTimeInterval(60 * 60)
        return Timeline(entries: entries, policy: .after(reloadAt))
    }
}
