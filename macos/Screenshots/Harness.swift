import AppKit
import SwiftUI

/// `make screenshots`: renders the real app and widget views against the anonymized demo
/// environment (`scripts/screenshots`) into PNGs for the README. Built with `-D SCREENSHOTS`,
/// so the app code runs without a daemon connection or other side effects.
///
///     harness --out docs/assets/screenshots --ansi <captures dir> [--only name,name]
@main
struct ScreenshotHarness {
    @MainActor
    static func main() {
        _ = ActiveApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let args = CommandLine.arguments
        func value(_ flag: String) -> String? {
            args.firstIndex(of: flag).flatMap { args.indices.contains($0 + 1) ? args[$0 + 1] : nil }
        }
        let out = URL(fileURLWithPath: value("--out") ?? "screenshots", isDirectory: true)
        try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let only = value("--only").map { Set($0.split(separator: ",").map(String.init)) }
        let icon = value("--icon").flatMap { NSImage(contentsOfFile: $0) }
        Shots(out: out, ansiDir: value("--ansi").map { URL(fileURLWithPath: $0, isDirectory: true) }, icon: icon, only: only).run()
    }
}

@MainActor
struct Shots {
    let out: URL
    let ansiDir: URL?
    let icon: NSImage?
    let only: Set<String>?

    func run() {
        let model = AppModel()
        model.snapshotStore.reload()
        model.showDaemonOnline()
        guard let snapshot = model.snapshot else {
            FileHandle.standardError.write(Data("no demo snapshot at \(model.snapshotStore.fileURL.path)\n".utf8))
            exit(1)
        }
        for appearance in Appearance.allCases {
            shot("widgets-\(appearance.rawValue)") { widgets(snapshot, appearance) }
            shot("menu-\(appearance.rawValue)") { menu(model, snapshot, appearance) }
            shot("notifications-\(appearance.rawValue)") { notifications(appearance) }
        }
        let settings = SettingsController(app: model)
        settings.windowAppeared()
        model.selectedProfileID = "work"
        for (name, page) in [("account", ProfilePage.general), ("limits", .limits), ("warmup", .warmup), ("statusline", .statusline)] {
            shot("settings-profile-\(name)") {
                settings.profilePage = page
                return settingsPane(model, settings, tab: .profiles, width: 800, height: 580) { ProfilesSettingsView() }
            }
        }
        // No General pane: it describes the harness binary (icon, version, login item), not the app.
        shot("settings-notifications") {
            settingsPane(model, settings, tab: .notifications, width: 520, height: 360) { NotificationsSettingsView() }
        }
        // No Advanced pane: its diagnostics show absolute paths of the demo environment.
        shot("settings-about") {
            NSApp.applicationIconImage = icon  // the harness is no app bundle; run.sh embeds the version
            return settingsPane(model, settings, tab: .about, width: 660, height: 600) { AboutSettingsView() }
        }
        terminals()
    }

    private func shot(_ name: String, _ make: () -> CGImage) {
        if let only, !only.contains(name), !only.contains(where: { name.hasPrefix($0) }) { return }
        Capture.write(make(), to: out.appendingPathComponent("\(name).png"))
    }

    // MARK: - Widgets

    private func display(_ snapshot: WidgetSnapshot, _ id: String) -> WidgetDisplay {
        WidgetDisplayBuilder.build(snapshot: snapshot, profileID: id, now: Date())
    }

    private func widgets(_ snapshot: WidgetSnapshot, _ appearance: Appearance) -> CGImage {
        let view = Desktop(appearance: appearance) {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 16) {
                    WidgetCard(size: CardSize.small, appearance: appearance) {
                        SmallWidgetView(display: display(snapshot, "client"), style: .bar)
                    }
                    WidgetCard(size: CardSize.small, appearance: appearance) {
                        SmallWidgetView(display: display(snapshot, "work"), style: .gauge)
                    }
                    WidgetCard(size: CardSize.medium, appearance: appearance) {
                        MediumWidgetView(display: display(snapshot, "personal"))
                    }
                }
                HStack(spacing: 16) {
                    WidgetCard(size: CardSize.large, appearance: appearance) {
                        LargeWidgetView(display: display(snapshot, "work"))
                    }
                    WidgetCard(size: CardSize.large, appearance: appearance) {
                        LargeWidgetView(display: display(snapshot, "personal"))
                    }
                }
            }
        }
        return Capture.swiftUI(view, appearance: appearance)
    }

    // MARK: - Menu bar

    private func menu(_ model: AppModel, _ snapshot: WidgetSnapshot, _ appearance: Appearance) -> CGImage {
        let content = Capture.window(
            MenuContentView().environment(model),
            appearance: appearance,
            settle: 0.8
        )
        let label = Capture.swiftUI(MenuLabelContent(snapshot: snapshot, mode: .letterPercent), appearance: appearance)
        let width = CGFloat(content.width) / Capture.scale + 300
        let view = VStack(alignment: .trailing, spacing: 6) {
            MenuBarStrip(label: label, width: width, appearance: appearance)
            MenuPanel(content: content, appearance: appearance)
                .padding(.trailing, 150)
                .padding(.bottom, 40)
        }
        .background(Desktop<EmptyView>.color(appearance))
        return Capture.swiftUI(view, appearance: appearance)
    }

    // MARK: - Notifications

    /// The newest notifying events of the demo state, newest first.
    private func notifications(_ appearance: Appearance) -> CGImage {
        let file = LaunchAgentEnvironment.stateDir.appendingPathComponent("events.jsonl")
        let lines = ((try? String(contentsOf: file, encoding: .utf8)) ?? "").split(separator: "\n")
        var banners: [(title: String, body: String, age: String)] = []
        for line in lines.reversed() {
            guard let object = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  let data = object["data"] as? [String: Any],
                  data["notify"] as? Bool == true,
                  let title = data["title"] as? String
            else { continue }
            let ts = (object["ts"] as? String).flatMap(SnapshotDecoding.parseDate)
            banners.append((title, data["body"] as? String ?? "", ts.map { Self.age($0) } ?? "now"))
            if banners.count == 4 { break }
        }
        let view = Desktop(appearance: appearance) {
            VStack(spacing: 10) {
                ForEach(Array(banners.enumerated()), id: \.offset) { _, banner in
                    NotificationBanner(icon: icon, title: banner.title, message: banner.body, age: banner.age, appearance: appearance)
                }
            }
        }
        return Capture.swiftUI(view, appearance: appearance)
    }

    /// Notification Center's relative time: `now`, `9m ago`, `2h ago`.
    private static func age(_ date: Date) -> String {
        let minutes = Int(Date().timeIntervalSince(date) / 60)
        if minutes < 1 { return "now" }
        if minutes < 60 { return "\(minutes)m ago" }
        return "\(minutes / 60)h ago"
    }

    // MARK: - Settings

    private func settingsPane<Content: View>(
        _ model: AppModel,
        _ settings: SettingsController,
        tab: SettingsTab,
        width: CGFloat,
        height: CGFloat,
        until ready: (() -> Bool)? = nil,
        @ViewBuilder content: () -> Content
    ) -> CGImage {
        let appearance = Appearance.light
        let pane = SettingsPane(width: width, height: height) { content() }
            .tint(.accentColor)
            .environment(model)
            .environment(settings)
        let image = Capture.window(pane, size: CGSize(width: width, height: height), appearance: appearance, settle: 4, until: ready)
        let title = SettingsToolbar.items.first { $0.0 == tab }?.1 ?? "Settings"
        let window = WindowFrame(title: title, content: image, appearance: appearance) {
            SettingsToolbar(selected: tab)
        }
        return Capture.swiftUI(Desktop(appearance: appearance) { window }, appearance: appearance)
    }

    // MARK: - Terminal

    /// The captures the README shows (`capture_cli.sh` records a few more).
    static let terminalShots: Set<String> = [
        "statusline_work", "launcher_prompt", "status", "sessions", "events", "auth_status", "doctor",
    ]

    private func terminals() {
        guard let ansiDir,
              let files = try? FileManager.default.contentsOfDirectory(at: ansiDir, includingPropertiesForKeys: nil)
        else { return }
        for file in files.filter({ $0.pathExtension == "ansi" }).sorted(by: { $0.path < $1.path }) {
            let name = file.deletingPathExtension().lastPathComponent
            guard Self.terminalShots.contains(name) else { continue }
            shot("terminal-\(name)") {
                let output = (try? String(contentsOf: file, encoding: .utf8)) ?? ""
                let cmdFile = file.deletingPathExtension().appendingPathExtension("cmd")
                let command = (try? String(contentsOf: cmdFile, encoding: .utf8))?
                    .trimmingCharacters(in: .whitespacesAndNewlines) ?? "ccs \(name)"
                let trimmed = output.hasSuffix("\n") ? String(output.dropLast()) : output
                let window = TerminalWindow(command: command, output: trimmed)
                return Capture.swiftUI(Desktop(appearance: .dark) { window }, appearance: .dark)
            }
        }
    }
}
