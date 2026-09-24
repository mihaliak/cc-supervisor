import SwiftUI
import WidgetKit

/// One CC Supervisor profile per widget; right-click → Edit Widget picks it.
struct ProfileUsageWidget: Widget {
    static let kind = "ProfileUsageWidget"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: Self.kind, intent: ProfileWidgetIntent.self, provider: UsageTimelineProvider()) { entry in
            ProfileUsageWidgetView(entry: entry)
        }
        .configurationDisplayName("Claude usage")
        .description("Session, weekly, model and extra usage for one CC Supervisor profile.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

struct ProfileUsageWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: UsageEntry

    var body: some View {
        content
            .containerBackground(.fill.tertiary, for: .widget)
            .widgetURL(entry.display.url)
    }

    @ViewBuilder private var content: some View {
        switch family {
        case .systemMedium:
            MediumWidgetView(display: entry.display)
        case .systemLarge, .systemExtraLarge:
            LargeWidgetView(display: entry.display)
        default:
            SmallWidgetView(display: entry.display)
        }
    }
}

@main
struct CCSupervisorWidgetsBundle: WidgetBundle {
    var body: some Widget {
        ProfileUsageWidget()
    }
}
