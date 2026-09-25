import SwiftUI

/// Warn/pause thresholds as one compact grid, plus the model-scoped and spill
/// toggles (ADR-0008).
struct LimitsSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    private static let windows: [(key: String, title: String)] = [
        ("session", "Session (5 h)"),
        ("weekly", "Weekly"),
        ("model_scoped", "Model (e.g. Fable)"),
        ("extra_usage", "Extra usage"),
    ]

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section {
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                GridRow {
                    Text("")
                    Text("Warn at").font(.caption).foregroundStyle(.secondary)
                    Text("Pause at").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(Self.windows, id: \.key) { window in
                    GridRow {
                        Text(window.title)
                        PercentField(title: "\(window.title) warn", value: store.intBinding(p("limits.\(window.key).warn")))
                        PercentField(title: "\(window.title) pause", value: store.intBinding(p("limits.\(window.key).pause")))
                    }
                }
            }
            IssueText(messages: Self.windows.flatMap { store.errors.messages(within: p("limits.\($0.key)")) })
        } header: {
            Text("Limits")
        } footer: {
            Text("Extra usage is % of the monthly credit cap; its pause applies only with Spill into credits.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }

        Section {
            Toggle(isOn: store.boolBinding(p("limits.model_scoped.warn_only"))) {
                Text("Fable warn-only")
                Text("Notify only; never pause sessions on that model.")
            }
            Toggle(isOn: store.boolBinding(p("limits.extra_usage.spill"))) {
                Text("Spill into credits")
                Text("With credits enabled, keep working past the session/weekly limits; pause near the credit cap instead.")
            }
        }
    }
}
