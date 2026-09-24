import SwiftUI

/// Placeholder until P11 fills the Settings window (General + Profiles tabs).
struct SettingsPlaceholderView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Form {
            Section("General") {
                Toggle("Launch at login", isOn: Binding(
                    get: { model.loginItemEnabled },
                    set: { model.setLoginItem($0) }
                ))
                LabeledContent("ccs path", value: model.config.ccsPath)
                LabeledContent("Menu bar", value: model.config.menuBarMode.rawValue)
            }
            Section("Profiles") {
                if let profiles = model.snapshot?.profiles, !profiles.isEmpty {
                    Picker("Profile", selection: $model.selectedProfileID) {
                        ForEach(profiles) { profile in
                            Text("\(profile.emoji) \(profile.name)").tag(Optional(profile.id))
                        }
                    }
                } else {
                    Text("No profiles yet.").foregroundStyle(.secondary)
                }
                Text("Profile editing arrives with the full Settings window. Until then use `ccs profile …` in a terminal.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .padding()
    }
}
