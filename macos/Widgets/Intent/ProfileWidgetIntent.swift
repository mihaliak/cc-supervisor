import AppIntents
import WidgetKit

/// Right-click → Edit Widget: which profile to show, and which rows (medium/large).
struct ProfileWidgetIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Claude usage"
    static let description = IntentDescription("Choose the CC Supervisor profile this widget shows.")

    @Parameter(title: "Profile", optionsProvider: ProfileOptionsProvider())
    var profileID: String?

    @Parameter(title: "Small widget style", optionsProvider: SmallStyleOptionsProvider())
    var smallStyle: String?

    @Parameter(title: "Show weekly", default: true)
    var showWeekly: Bool

    @Parameter(title: "Show model limits", default: true)
    var showModelScoped: Bool

    @Parameter(title: "Show extra usage", default: true)
    var showExtraUsage: Bool

    init() {}

    /// Small widgets offer the style; medium and large offer the row toggles.
    static var parameterSummary: some ParameterSummary {
        When(widgetFamily: .equalTo, .systemSmall) {
            Summary {
                \.$profileID
                \.$smallStyle
            }
        } otherwise: {
            Summary {
                \.$profileID
                \.$showWeekly
                \.$showModelScoped
                \.$showExtraUsage
            }
        }
    }

    var style: SmallWidgetStyle { SmallWidgetStyle(configValue: smallStyle) }

    var options: WidgetOptions {
        WidgetOptions(showWeekly: showWeekly, showModelScoped: showModelScoped, showExtraUsage: showExtraUsage)
    }
}
