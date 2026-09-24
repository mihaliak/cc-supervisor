import SwiftUI

/// Profiles tab: list (emoji, name, sign-in dot) with + / −, and the detail editor.
struct ProfilesSplitView: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings
    @State private var showingAdd = false
    @State private var removing: ConfigModel.Profile?

    var body: some View {
        @Bindable var app = app
        let store = settings.store
        NavigationSplitView {
            List(selection: $app.selectedProfileID) {
                ForEach(store.profiles) { profile in
                    ProfileRow(profile: profile, isDefault: profile.id == store.defaultProfileID)
                        .tag(Optional(profile.id))
                }
            }
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 4) {
                    Button {
                        showingAdd = true
                    } label: {
                        Image(systemName: "plus")
                    }
                    .help("Add a profile")
                    Button {
                        removing = store.profiles.first { $0.id == app.selectedProfileID }
                    } label: {
                        Image(systemName: "minus")
                    }
                    .help("Remove the selected profile")
                    .disabled(app.selectedProfileID == nil)
                    Spacer()
                }
                .buttonStyle(.borderless)
                .padding(8)
                .disabled(!store.canEdit)
            }
            .navigationSplitViewColumnWidth(min: 190, ideal: 210)
        } detail: {
            if let id = app.selectedProfileID, store.profiles.contains(where: { $0.id == id }) {
                ProfileDetailView(profileID: id)
                    .id(id)
            } else {
                ContentUnavailableView(
                    store.profiles.isEmpty ? "No profiles" : "Select a profile",
                    systemImage: "person.crop.circle",
                    description: Text(store.profiles.isEmpty ? "Add one with +." : "")
                )
            }
        }
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

private struct ProfileRow: View {
    @Environment(SettingsController.self) private var settings
    let profile: ConfigModel.Profile
    let isDefault: Bool

    var body: some View {
        HStack(spacing: 6) {
            Text(profile.emoji ?? "•")
            VStack(alignment: .leading, spacing: 0) {
                Text(profile.displayName)
                Text("ccs --\(profile.flag ?? profile.id)" + (isDefault ? " · default" : ""))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if settings.store.errors.hasIssues(profileID: profile.id) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .help("This profile has invalid settings")
            }
            Circle()
                .fill(authColor)
                .frame(width: 8, height: 8)
                .help(authHelp)
        }
    }

    private var authColor: Color {
        switch settings.authStatus[profile.id]?.loggedIn {
        case true?: .green
        case false?: .orange
        case nil: .gray.opacity(0.4)
        }
    }

    private var authHelp: String {
        switch settings.authStatus[profile.id]?.loggedIn {
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
        VStack(alignment: .leading, spacing: 12) {
            Text("Add profile").font(.headline)
            Form {
                TextField("ID", text: $draft.id, prompt: Text("work"))
                Text("Lowercase letters, digits and -. Can't be changed later.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("Launcher flag", text: $draft.flag, prompt: Text(draft.id.isEmpty ? "same as ID" : draft.id))
                TextField("Name", text: $draft.name, prompt: Text("Work"))
                EmojiField(title: "Emoji", text: $draft.emoji)
                DirectoryField(title: "Claude config dir", path: $draft.configDir, placeholder: "~/.claude-work")
                Toggle("Default profile (`ccs` with no flag)", isOn: $draft.makeDefault)
            }
            .formStyle(.grouped)
            if let error {
                Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add") {
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
        }
        .padding()
        .frame(width: 520)
    }
}
