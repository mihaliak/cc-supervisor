import SwiftUI

/// Profiles pane (ADR-0020): a fixed profile list with + / − under it, the editor on
/// the right (the Mail "Accounts" pattern). Deliberately not a `NavigationSplitView`:
/// inside a Settings window that takes over the toolbar, hiding the pane tabs.
struct ProfilesSettingsView: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings
    @State private var showingAdd = false
    @State private var removing: ConfigModel.Profile?

    var body: some View {
        let store = settings.store
        HStack(alignment: .top, spacing: 16) {
            ProfileList(
                onAdd: { showingAdd = true },
                onRemove: { removing = store.profiles.first { $0.id == app.selectedProfileID } }
            )
            .frame(width: 200)

            Group {
                if let id = app.selectedProfileID, store.profiles.contains(where: { $0.id == id }) {
                    ProfileDetailView(profileID: id)
                        .id(id)
                } else {
                    ContentUnavailableView(
                        store.profiles.isEmpty ? "No Profiles" : "No Profile Selected",
                        systemImage: "person.crop.circle",
                        description: Text(store.profiles.isEmpty ? "Add one with +." : "Select a profile on the left.")
                    )
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding(16)
        .onAppear {
            for profile in store.profiles where settings.authStatus[profile.id] == nil {
                settings.refreshAuth(profile.id)
            }
        }
        .sheet(isPresented: $showingAdd) {
            AddProfileSheet()
        }
        .confirmationDialog(
            "Remove profile \(removing?.displayName ?? "")?",
            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
            presenting: removing
        ) { profile in
            Button("Remove", role: .destructive) { settings.removeProfile(profile.id) }
        } message: { profile in
            Text("Only the CC Supervisor profile is removed. The Claude config dir \(profile.configDir ?? "") and its sign-in are not deleted.")
        }
    }
}

/// The bordered list with the standard add/remove bar attached below it.
private struct ProfileList: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings
    let onAdd: () -> Void
    let onRemove: () -> Void

    var body: some View {
        @Bindable var app = app
        let store = settings.store
        VStack(spacing: 0) {
            List(selection: $app.selectedProfileID) {
                ForEach(store.profiles) { profile in
                    ProfileRow(profile: profile, isDefault: profile.id == store.defaultProfileID)
                        .tag(Optional(profile.id))
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)

            Divider()
            HStack(spacing: 0) {
                barButton("plus", help: "Add a profile", action: onAdd)
                Divider().frame(height: 14)
                barButton("minus", help: "Remove the selected profile", action: onRemove)
                    .disabled(app.selectedProfileID == nil)
                Spacer()
            }
            .frame(height: 24)
            .disabled(!store.canEdit)
        }
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color(nsColor: .separatorColor)))
    }

    private func barButton(_ systemImage: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .frame(width: 26, height: 22)
                .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .help(help)
    }
}

private struct ProfileRow: View {
    @Environment(SettingsController.self) private var settings
    let profile: ConfigModel.Profile
    let isDefault: Bool

    var body: some View {
        HStack(spacing: 8) {
            Text(profile.emoji ?? "•")
                .font(.title3)
            VStack(alignment: .leading, spacing: 0) {
                Text(profile.displayName)
                Text("--\(profile.flag ?? profile.id)" + (isDefault ? " · default" : ""))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)
            if settings.store.errors.hasIssues(profileID: profile.id) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .help("This profile has invalid settings")
            }
            AuthDot(status: settings.authStatus[profile.id])
        }
        .padding(.vertical, 2)
    }
}

/// Green = signed in, orange = signed out, gray = unknown.
struct AuthDot: View {
    let status: AuthStatusResult?

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .help(help)
    }

    private var color: Color {
        switch status?.loggedIn {
        case true?: .green
        case false?: .orange
        case nil: .gray.opacity(0.4)
        }
    }

    private var help: String {
        switch status?.loggedIn {
        case true?: "Signed in"
        case false?: "Signed out"
        case nil: "Sign-in status unknown"
        }
    }
}

/// `ccs profile add … --json`; Python fills every other default (ADR-0004).
struct AddProfileSheet: View {
    @Environment(SettingsController.self) private var settings
    @Environment(\.dismiss) private var dismiss
    @State private var draft = SettingsController.NewProfile()
    @State private var error: String?
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Form {
                Section {
                    TextField("ID", text: $draft.id, prompt: Text("work"))
                    TextField("Launcher flag", text: $draft.flag, prompt: Text(draft.id.isEmpty ? "same as ID" : draft.id))
                    TextField("Name", text: $draft.name, prompt: Text("Work"))
                    EmojiField(title: "Emoji", text: $draft.emoji)
                    DirectoryField(title: "Claude config dir", path: $draft.configDir, placeholder: "~/.claude-work")
                    Toggle("Default profile", isOn: $draft.makeDefault)
                } header: {
                    Text("New Profile")
                } footer: {
                    Text("ID: lowercase letters, digits and -; can't be changed later. The default profile is what `ccs` with no flag starts.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(true)

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add Profile") {
                    working = true
                    Task {
                        error = await settings.addProfile(draft)
                        working = false
                        if error == nil { dismiss() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(working || draft.id.isEmpty || draft.emoji.isEmpty || draft.configDir.isEmpty)
            }
            .padding([.horizontal, .bottom], 20)
        }
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
    }
}
