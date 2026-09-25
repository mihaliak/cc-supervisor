import SwiftUI

/// Sign-in status and actions (`ccs auth status|login|logout`, ADR-0003).
struct AccountSection: View {
    @Environment(SettingsController.self) private var settings
    @State private var confirmSignOut = false
    let profileID: String

    var body: some View {
        let status = settings.authStatus[profileID]
        let signedIn = status?.loggedIn == true
        Section {
            LabeledContent("Status") {
                HStack(spacing: 6) {
                    if let status {
                        Text(status.loggedIn == true ? "Signed in" : "Signed out")
                            .foregroundStyle(status.loggedIn == true ? Color.primary : Color.orange)
                    } else if let error = settings.authErrors[profileID] {
                        Text(error).foregroundStyle(.red)
                    } else {
                        ProgressView().controlSize(.small)
                    }
                    Button {
                        settings.refreshAuth(profileID)
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .help("Refresh sign-in status")
                }
            }
            if let account = status?.account, signedIn {
                LabeledContent("Account", value: account)
            }
            if let subscription = status?.subscriptionType, signedIn {
                LabeledContent("Plan", value: subscription)
            }
            HStack {
                if settings.isSigningIn(profileID) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for the browser…").foregroundStyle(.secondary)
                    Spacer()
                    Button("Cancel") { settings.cancelSignIn(profileID) }
                } else {
                    Spacer()
                    Button("Sign Out…") { confirmSignOut = true }
                        .disabled(!signedIn || settings.isBusy("signout:\(profileID)"))
                    Button(signedIn ? "Sign In Again…" : "Sign In…") {
                        settings.signIn(profileID)
                    }
                }
            }
            MessageLine(message: settings.message(profileID))
        } header: {
            Text("Claude account")
        } footer: {
            VStack(alignment: .leading, spacing: 2) {
                Text("Claude Code's own claude.ai login for this config dir; plain `claude` with this dir uses it too.")
                if let service = status?.keychainService {
                    Text("Keychain item: \(service)")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .confirmationDialog("Sign out of this profile?", isPresented: $confirmSignOut) {
            Button("Sign Out", role: .destructive) { settings.signOut(profileID) }
        } message: {
            Text("Claude Code in this config dir will be signed out too.")
        }
    }
}
