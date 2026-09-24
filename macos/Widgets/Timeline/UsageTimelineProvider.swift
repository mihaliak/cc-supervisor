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
        let profileID = configuration.profile?.id
        // Widget gallery without real data for this profile: show the sample.
        if context.isPreview, snapshot?.profile(id: profileID ?? "") == nil {
            return UsageEntry.sample(now: now)
        }
        return UsageEntry(
            date: now,
            display: WidgetDisplayBuilder.build(
                snapshot: snapshot, profileID: profileID, options: configuration.options, now: now
            )
        )
    }

    func timeline(for configuration: ProfileWidgetIntent, in context: Context) async -> Timeline<UsageEntry> {
        let now = Date()
        let snapshot = try? SnapshotLoader.load().get()
        let entries = WidgetTimeline.items(
            snapshot: snapshot,
            profileID: configuration.profile?.id,
            options: configuration.options,
            now: now
        ).map(UsageEntry.init)
        let reloadAt = entries.last?.date ?? now.addingTimeInterval(60 * 60)
        return Timeline(entries: entries, policy: .after(reloadAt))
    }
}
