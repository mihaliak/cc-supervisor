#if DEBUG
import SwiftUI
import WidgetKit

/// Preview entries for every state (built from `WidgetSamples`, mirroring the
/// `schema/fixtures/snapshot/` variants).
enum PreviewEntries {
    static let now = Date()

    static let ok = UsageEntry.sample(now: now)
    static let paused = UsageEntry.sample(now: now, paused: true)
    static let signIn = UsageEntry.sample(now: now, status: .needsSignIn)
    static let stale = UsageEntry.sample(now: now, status: .stale)
    static let offline = UsageEntry(
        date: now,
        display: WidgetDisplayBuilder.build(
            snapshot: WidgetSamples.snapshot(now: now.addingTimeInterval(-15 * 60)),
            profileID: WidgetSamples.profileID,
            now: now
        )
    )
    static let notConfigured = UsageEntry(
        date: now,
        display: WidgetDisplayBuilder.build(snapshot: nil, profileID: nil, now: now)
    )
}

#Preview("Small", as: .systemSmall) {
    ProfileUsageWidget()
} timeline: {
    PreviewEntries.ok
    PreviewEntries.paused
    PreviewEntries.signIn
    PreviewEntries.stale
    PreviewEntries.offline
    PreviewEntries.notConfigured
}

#Preview("Small · gauge", as: .systemSmall) {
    ProfileUsageWidget()
} timeline: {
    UsageEntry(date: PreviewEntries.ok.date, display: PreviewEntries.ok.display, smallStyle: .gauge)
    UsageEntry(date: PreviewEntries.paused.date, display: PreviewEntries.paused.display, smallStyle: .gauge)
}

#Preview("Medium", as: .systemMedium) {
    ProfileUsageWidget()
} timeline: {
    PreviewEntries.ok
    PreviewEntries.paused
    PreviewEntries.signIn
    PreviewEntries.stale
    PreviewEntries.offline
    PreviewEntries.notConfigured
}

#Preview("Large", as: .systemLarge) {
    ProfileUsageWidget()
} timeline: {
    PreviewEntries.ok
    PreviewEntries.paused
    PreviewEntries.signIn
    PreviewEntries.stale
    PreviewEntries.offline
    PreviewEntries.notConfigured
}
#endif
