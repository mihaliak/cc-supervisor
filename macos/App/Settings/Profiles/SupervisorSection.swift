import SwiftUI

/// Supervision on/off and the resume prompt typed in after a reset (ADR-0007/0008).
struct SupervisorSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section {
            Toggle("Pause `ccs` sessions at the limits", isOn: store.boolBinding(p("supervisor.enabled"), fallback: true))
            TextField("Resume prompt", text: store.stringBinding(p("supervisor.resume_prompt")), axis: .vertical)
                .lineLimit(2...4)
            IssueText(messages: store.issues(p("supervisor.resume_prompt")))
        } header: {
            Text("Supervisor")
        } footer: {
            Text("Off: warnings and warm-ups only. The resume prompt is typed into sessions that were interrupted mid-work, once the limit resets.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}
