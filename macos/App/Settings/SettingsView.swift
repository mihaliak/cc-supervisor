import SwiftUI

/// The Settings window (ADR-0020): a root `TabView`, so macOS shows standard toolbar
/// tabs and titles the window after the selected pane.
struct SettingsView: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings

    var body: some View {
        @Bindable var settings = settings
        let store = settings.store
        TabView(selection: $settings.selectedTab) {
            Tab("General", systemImage: "gearshape", value: SettingsTab.general) {
                SettingsPane(width: 520, height: 400) { GeneralSettingsView() }
            }
            Tab("Profiles", systemImage: "person.2", value: SettingsTab.profiles) {
                SettingsPane(width: 800, height: 580) { ProfilesSettingsView() }
            }
            Tab("Notifications", systemImage: "bell.badge", value: SettingsTab.notifications) {
                SettingsPane(width: 520, height: 360) { NotificationsSettingsView() }
            }
            Tab("Advanced", systemImage: "gearshape.2", value: SettingsTab.advanced) {
                SettingsPane(width: 620, height: 560) { AdvancedSettingsView() }
            }
            Tab("About", systemImage: "info.circle", value: SettingsTab.about) {
                SettingsPane(width: 660, height: 600) { AboutSettingsView() }
            }
        }
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

/// One pane: config banners (only when there is something to say) above the content,
/// at a fixed size per pane (settings windows aren't user-resizable).
struct SettingsPane<Content: View>: View {
    let width: CGFloat
    let height: CGFloat
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 0) {
            ConfigBanners()
            content
        }
        .frame(width: width, height: height)
    }
}

/// Config file state: missing, unreadable, invalid, save errors.
private struct ConfigBanners: View {
    @Environment(SettingsController.self) private var settings

    var body: some View {
        let store = settings.store
        let hasAny = !store.fileExists || store.loadError != nil || store.saveError != nil
            || store.isInvalid || (store.validationProblem != nil && store.fileExists)
            || settings.message("general") != nil
        if hasAny {
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
            .padding(.horizontal, 20)
            .padding(.top, 12)
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
