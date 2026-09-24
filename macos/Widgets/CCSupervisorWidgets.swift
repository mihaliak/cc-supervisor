import SwiftUI
import WidgetKit

struct PlaceholderEntry: TimelineEntry {
    let date: Date
}

struct PlaceholderProvider: TimelineProvider {
    func placeholder(in context: Context) -> PlaceholderEntry {
        PlaceholderEntry(date: .now)
    }

    func getSnapshot(in context: Context, completion: @escaping @Sendable (PlaceholderEntry) -> Void) {
        completion(PlaceholderEntry(date: .now))
    }

    func getTimeline(
        in context: Context,
        completion: @escaping @Sendable (Timeline<PlaceholderEntry>) -> Void
    ) {
        completion(Timeline(entries: [PlaceholderEntry(date: .now)], policy: .never))
    }
}

struct PlaceholderWidgetView: View {
    let entry: PlaceholderEntry

    var body: some View {
        Text("CC Supervisor")
            .containerBackground(.fill.tertiary, for: .widget)
    }
}

struct PlaceholderWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "local.ccsupervisor.placeholder", provider: PlaceholderProvider()) {
            PlaceholderWidgetView(entry: $0)
        }
        .configurationDisplayName("CC Supervisor")
        .description("Placeholder widget.")
        .supportedFamilies([.systemSmall])
    }
}

@main
struct CCSupervisorWidgets: WidgetBundle {
    var body: some Widget {
        PlaceholderWidget()
    }
}
