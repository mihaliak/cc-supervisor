import AppIntents
import WidgetKit

/// Right-click → Edit Widget: which profile to show, and which rows (medium/large).
struct ProfileWidgetIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Claude usage"
    static let description = IntentDescription("Choose the CC Supervisor profile this widget shows.")

    @Parameter(title: "Profile", optionsProvider: ProfileOptionsProvider())
    var profileID: String?

    @Parameter(title: "Show weekly", default: true)
    var showWeekly: Bool

    @Parameter(title: "Show model limits", default: true)
    var showModelScoped: Bool

    @Parameter(title: "Show extra usage", default: true)
    var showExtraUsage: Bool

    init() {}

    var options: WidgetOptions {
        WidgetOptions(showWeekly: showWeekly, showModelScoped: showModelScoped, showExtraUsage: showExtraUsage)
    }
}
