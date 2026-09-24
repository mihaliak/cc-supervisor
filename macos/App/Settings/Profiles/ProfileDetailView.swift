import SwiftUI

/// The profile editor: one `Section` per ADR-0004 group.
struct ProfileDetailView: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        Form {
            let profileIssues = store.issues(.profile(profileID, ""))
            if !profileIssues.isEmpty {
                IssueText(messages: profileIssues)
            }
            IdentitySection(profileID: profileID)
            AccountSection(profileID: profileID)
            LimitsSection(profileID: profileID)
            SupervisorSection(profileID: profileID)
            StatuslineSection(profileID: profileID)
            WarmupSection(profileID: profileID)
        }
        .formStyle(.grouped)
        .disabled(!store.canEdit)
        .onAppear { settings.profileAppeared(profileID) }
        .onChange(of: store.loadedRevision) { _, _ in
            settings.refreshPreview(profileID)
        }
    }
}
