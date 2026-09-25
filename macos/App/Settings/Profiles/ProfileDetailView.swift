import SwiftUI

/// The profile editor (ADR-0020): a header, then a segmented control switching between
/// compact pages (the Mail account-detail pattern) instead of one long form.
struct ProfileDetailView: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        @Bindable var settings = settings
        let store = settings.store
        VStack(spacing: 10) {
            ProfileHeader(profileID: profileID)
            Picker("Page", selection: $settings.profilePage) {
                ForEach(ProfilePage.allCases, id: \.self) { page in
                    Text(page.title).tag(page)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()

            Form {
                let profileIssues = store.issues(.profile(profileID, ""))
                if !profileIssues.isEmpty {
                    Section { IssueText(messages: profileIssues) }
                }
                switch settings.profilePage {
                case .general:
                    IdentitySection(profileID: profileID)
                    AccountSection(profileID: profileID)
                case .limits:
                    SupervisorSection(profileID: profileID)
                    LimitsSection(profileID: profileID)
                case .warmup:
                    WarmupSection(profileID: profileID)
                case .statusline:
                    StatuslineSection(profileID: profileID)
                }
            }
            .formStyle(.grouped)
            .disabled(!store.canEdit)
        }
        .onAppear { settings.profileAppeared(profileID) }
        .onChange(of: store.loadedRevision) { _, _ in
            settings.refreshPreview(profileID)
        }
    }
}

/// Emoji, name, launcher flag and sign-in state of the selected profile.
private struct ProfileHeader: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let profile = store.profiles.first { $0.id == profileID }
        let status = settings.authStatus[profileID]
        HStack(spacing: 10) {
            Text(profile?.emoji ?? "•")
                .font(.system(size: 28))
            VStack(alignment: .leading, spacing: 1) {
                Text(profile?.displayName ?? profileID)
                    .font(.headline)
                Text("ccs --\(profile?.flag ?? profileID)" + (store.defaultProfileID == profileID ? " · default profile" : ""))
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Spacer()
            HStack(spacing: 5) {
                AuthDot(status: status)
                Text(authText(status))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 4)
    }

    private func authText(_ status: AuthStatusResult?) -> String {
        guard let status else { return "Checking sign-in…" }
        guard status.loggedIn == true else { return "Signed out" }
        return status.subscriptionType.map { "Signed in · \($0)" } ?? "Signed in"
    }
}
