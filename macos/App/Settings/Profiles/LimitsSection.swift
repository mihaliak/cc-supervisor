import SwiftUI

/// Warn/pause thresholds per window plus the model-scoped and spill toggles (ADR-0008).
struct LimitsSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    private static let windows: [(key: String, title: String)] = [
        ("session", "Session (5 h)"),
        ("weekly", "Weekly"),
        ("model_scoped", "Model-scoped (e.g. Fable)"),
        ("extra_usage", "Extra usage (% of monthly cap)"),
    ]

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section("Limits") {
            ForEach(Self.windows, id: \.key) { window in
                VStack(alignment: .leading, spacing: 4) {
                    Text(window.title).font(.subheadline.weight(.semibold))
                    PercentStepper(title: "Warn at", value: store.intBinding(p("limits.\(window.key).warn")))
                    PercentStepper(title: "Pause at", value: store.intBinding(p("limits.\(window.key).pause")))
                    IssueText(messages: store.errors.messages(within: p("limits.\(window.key)")))
                }
            }
            Toggle(isOn: store.boolBinding(p("limits.model_scoped.warn_only"))) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Fable warn-only (model-scoped limits)")
                    Text("Off: pause sessions on that model at the pause threshold. On: notify only; other models stay usable.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Toggle(isOn: store.boolBinding(p("limits.extra_usage.spill"))) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Spill into credits (extra usage)")
                    Text("On (and credits enabled on the account): don't pause at the session/weekly limits, keep working on credits, and pause at the extra-usage pause % of the monthly cap instead. Off: pause as normal.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
