import SwiftUI

/// The Settings window (P11): General and Profiles tabs over `config.json` and `ccs`.
struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings

    var body: some View {
        @Bindable var settings = settings
        let store = settings.store
        VStack(spacing: 0) {
            SettingsBanners()
                .padding(.horizontal, 12)
                .padding(.top, 8)
            TabView(selection: $settings.selectedTab) {
                Tab("General", systemImage: "gearshape", value: SettingsTab.general) {
                    GeneralSettingsView()
                }
                Tab("Profiles", systemImage: "person.2", value: SettingsTab.profiles) {
                    ProfilesSplitView()
                }
            }
        }
        .frame(width: 820, height: 660)
        .onAppear { settings.windowAppeared() }
        .onDisappear { settings.windowDisappeared() }
        .onChange(of: app.settingsRequest) { _, _ in settings.handleSettingsRequest() }
        .alert(
            "Settings changed elsewhere",
            isPresented: Binding(get: { store.conflict != nil }, set: { _ in })
        ) {
            Button("Keep mine") { Task { await store.resolveConflict(.keepMine) } }
            Button("Take theirs", role: .cancel) { Task { await store.resolveConflict(.takeTheirs) } }
        } message: {
            let fields = store.conflict?.fields.map(\.description).joined(separator: ", ") ?? ""
            Text("config.json was changed by another program (e.g. `ccs`) while you edited the same setting: \(fields). Keep your values, or take the values on disk?")
        }
        .sheet(isPresented: $settings.showingLogs) {
            DaemonLogsSheet(result: settings.logs)
        }
    }
}

/// Config file state: missing, unreadable, invalid, save errors.
private struct SettingsBanners: View {
    @Environment(SettingsController.self) private var settings

    var body: some View {
        let store = settings.store
        VStack(spacing: 6) {
            if !store.fileExists {
                HStack {
                    SettingsBanner(
                        text: "No config yet at \(store.fileURL.path).",
                        systemImage: "doc.badge.plus",
                        tint: .orange
                    )
                    Button("Create config") { settings.createConfig() }
                        .disabled(settings.isBusy("create-config"))
                }
            }
            if let error = store.loadError {
                SettingsBanner(text: error)
            }
            if let error = store.saveError {
                SettingsBanner(text: error)
            }
            if store.isInvalid {
                SettingsBanner(text: invalidText(store))
            }
            if let problem = store.validationProblem, store.fileExists {
                SettingsBanner(text: "Couldn't validate the config: \(problem)", systemImage: "questionmark.circle", tint: .orange)
            }
            MessageLine(message: settings.message("general"))
        }
    }

    private func invalidText(_ store: ConfigStore) -> String {
        var text = "Config invalid: the daemon keeps using the previous version until it's fixed."
        let top = store.errors.topLevel.map { $0.field.dotted.isEmpty ? $0.message : "\($0.field.dotted): \($0.message)" }
        if !top.isEmpty { text += " " + top.joined(separator: "; ") }
        return text
    }
}

private struct DaemonLogsSheet: View {
    let result: DaemonLogsResult?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Daemon log").font(.headline)
            if let path = result?.path {
                Text(path).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
            }
            ScrollView {
                Text((result?.lines ?? ["No log lines yet."]).joined(separator: "\n"))
                    .font(.caption.monospaced())
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(minHeight: 320)
            HStack {
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding()
        .frame(width: 720, height: 460)
    }
}
