import SwiftUI

/// Sign-in status and actions (`ccs auth status|login|logout`, ADR-0003).
struct AccountSection: View {
    @Environment(SettingsController.self) private var settings
    @State private var confirmSignOut = false
    let profileID: String

    var body: some View {
        let status = settings.authStatus[profileID]
        Section("Account") {
            LabeledContent("Status") {
                if let status {
                    Text(status.loggedIn == true ? "Signed in" : "Signed out")
                        .foregroundStyle(status.loggedIn == true ? Color.primary : Color.orange)
                } else if let error = settings.authErrors[profileID] {
                    Text(error).foregroundStyle(.red)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
            if let account = status?.account, status?.loggedIn == true {
                LabeledContent("Account", value: account)
            }
            if let subscription = status?.subscriptionType, status?.loggedIn == true {
                LabeledContent("Subscription", value: subscription)
            }
            if let service = status?.keychainService {
                LabeledContent("Keychain item") {
                    Text(service).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            HStack {
                if settings.isSigningIn(profileID) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for sign-in…").foregroundStyle(.secondary)
                    Button("Cancel") { settings.cancelSignIn(profileID) }
                } else {
                    Button(status?.loggedIn == true ? "Sign in again…" : "Sign in…") {
                        settings.signIn(profileID)
                    }
                    Button("Sign out…", role: .destructive) { confirmSignOut = true }
                        .disabled(status?.loggedIn != true || settings.isBusy("signout:\(profileID)"))
                }
                Spacer()
                Button {
                    settings.refreshAuth(profileID)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh sign-in status")
            }
            Text("Sign-in is Claude Code's own claude.ai login for this config dir. Plain `claude` with this dir uses it too.")
                .font(.caption)
                .foregroundStyle(.secondary)
            MessageLine(message: settings.message(profileID))
        }
        .confirmationDialog("Sign out of this profile?", isPresented: $confirmSignOut) {
            Button("Sign out", role: .destructive) { settings.signOut(profileID) }
        } message: {
            Text("Claude Code in this config dir will be signed out too.")
        }
    }
}
