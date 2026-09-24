import SwiftUI

/// id (read-only), launcher flag, name, emoji, config dir, default profile.
struct IdentitySection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        Section("Identity") {
            LabeledContent("ID") {
                Text(profileID).font(.body.monospaced()).textSelection(.enabled)
            }
            LabeledContent("Launcher flag") {
                HStack(spacing: 6) {
                    TextField("Launcher flag", text: store.stringBinding(p("flag")))
                        .labelsHidden()
                        .font(.body.monospaced())
                        .frame(maxWidth: 180)
                    Text("ccs --\(store.string(p("flag")) ?? profileID)")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            IssueText(messages: store.issues(p("flag")) + store.issues(p("id")))
            TextField("Name", text: store.stringBinding(p("name")))
            IssueText(messages: store.issues(p("name")))
            EmojiField(title: "Emoji", text: store.stringBinding(p("emoji")))
            IssueText(messages: store.issues(p("emoji")))
            DirectoryField(title: "Claude config dir", path: store.stringBinding(p("config_dir")), placeholder: "~/.claude")
            IssueText(messages: store.issues(p("config_dir")))
            LabeledContent("Default profile") {
                if store.defaultProfileID == profileID {
                    Text("Yes · `ccs` with no flag starts this profile")
                        .foregroundStyle(.secondary)
                } else {
                    Button("Make default") { store.set(.root("default_profile"), .string(profileID)) }
                }
            }
        }
    }
}
