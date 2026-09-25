import SwiftUI

/// Name, emoji, launcher flag, config dir, default profile. The id is read-only.
struct IdentitySection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section {
            TextField("Name", text: store.stringBinding(p("name")))
            IssueText(messages: store.issues(p("name")))
            EmojiField(title: "Emoji", text: store.stringBinding(p("emoji")))
            IssueText(messages: store.issues(p("emoji")))
            TextField("Launcher flag", text: store.stringBinding(p("flag")), prompt: Text(profileID))
            IssueText(messages: store.issues(p("flag")) + store.issues(p("id")))
            DirectoryField(title: "Claude config dir", path: store.stringBinding(p("config_dir")), placeholder: "~/.claude")
            IssueText(messages: store.issues(p("config_dir")))
            Toggle("Default profile", isOn: Binding(
                get: { store.defaultProfileID == profileID },
                set: { isOn in if isOn { store.set(.root("default_profile"), .string(profileID)) } }
            ))
            .disabled(store.defaultProfileID == profileID)
        } header: {
            Text("Profile")
        } footer: {
            Text("ID `\(profileID)` can't be changed. The default profile is what `ccs` with no flag starts.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}
