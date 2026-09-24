import SwiftUI

/// Supervision on/off and the resume prompt typed in after a reset (ADR-0007/0008).
struct SupervisorSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section("Supervisor") {
            Toggle(isOn: store.boolBinding(p("supervisor.enabled"), fallback: true)) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Supervise `ccs` sessions")
                    Text("Off: warnings and warm-ups only, never pauses.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Resume prompt")
                Text("Typed into sessions that were interrupted mid-work, once the limit resets.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextEditor(text: store.stringBinding(p("supervisor.resume_prompt")))
                    .font(.body)
                    .frame(minHeight: 54)
                    .scrollContentBackground(.hidden)
                    .padding(4)
                    .background(.background.secondary, in: RoundedRectangle(cornerRadius: 6))
                IssueText(messages: store.issues(p("supervisor.resume_prompt")))
            }
        }
    }
}
