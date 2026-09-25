import AppKit
import SwiftUI

/// General tab: ccs path, menu bar, notifications, login item, daemon, diagnostics, display.
struct GeneralSettingsView: View {
    @Environment(AppModel.self) private var app
    @Environment(SettingsController.self) private var settings

    var body: some View {
        let store = settings.store
        Form {
            Section("ccs") {
                LabeledContent("Path") {
                    HStack(spacing: 6) {
                        CommitTextField(
                            title: "Path",
                            text: store.stringBinding(.root("ccs_path"), emptyIsNull: true),
                            prompt: "~/.local/bin/ccs"
                        )
                        .labelsHidden()
                        .font(.body.monospaced())
                        Button("Browse…") { settings.browseForCcs() }
                    }
                }
                .disabled(!store.canEdit)
                IssueText(messages: store.issues(.root("ccs_path")))
                LabeledContent("Version") {
                    if let version = settings.ccsVersion {
                        Text(version).textSelection(.enabled)
                    } else if let error = settings.ccsVersionError {
                        Text(error).foregroundStyle(.red)
                    } else {
                        ProgressView().controlSize(.small)
                    }
                }
            }

            Section("Menu bar") {
                let menuBar = store.stringBinding(.root("display.menu_bar"))
                Picker("Label", selection: Binding(
                    get: { MenuBarMode.normalized(menuBar.wrappedValue) },
                    set: { menuBar.wrappedValue = $0 }
                )) {
                    Text("Letter + session/weekly %").tag(MenuBarMode.letterPercent.rawValue)
                    Text("Icon only").tag(MenuBarMode.iconOnly.rawValue)
                }
                .disabled(!store.canEdit)
                IssueText(messages: store.issues(.root("display.menu_bar")))
            }

            Section("Notifications") {
                ForEach(Self.notificationToggles, id: \.key) { toggle in
                    Toggle(toggle.title, isOn: store.boolBinding(.root("notifications.\(toggle.key)"), fallback: true))
                        .disabled(!store.canEdit)
                }
                Button("Open System Settings › Notifications") {
                    if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                        NSWorkspace.shared.open(url)
                    }
                }
            }

            Section("Login item") {
                Toggle("Launch CC Supervisor at login", isOn: Binding(
                    get: { app.loginItemEnabled },
                    set: { app.setLoginItem($0) }
                ))
                Text("Status: \(LoginItemController.statusDescription)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Daemon") {
                LabeledContent("Status") {
                    Text(daemonText).foregroundStyle(daemonColor)
                }
                HStack {
                    Button("Install") { settings.daemon(.install) }
                    Button("Start") { settings.daemon(.start) }
                    Button("Stop") { settings.daemon(.stop) }
                    Button("Restart") { settings.daemon(.restart) }
                    Spacer()
                    Button("Show logs") { settings.showLogs() }
                    Button {
                        settings.refreshDaemonStatus()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .help("Refresh daemon status")
                }
                .disabled(settings.isBusy("daemon") || settings.isBusy("logs"))
                MessageLine(message: settings.message("daemon"))
            }

            Section("Diagnostics") {
                HStack {
                    Button("Run diagnostics") { settings.runDoctor() }
                        .disabled(settings.isBusy("doctor"))
                    if settings.isBusy("doctor") { ProgressView().controlSize(.small) }
                }
                if let error = settings.doctorError {
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
                if let doctor = settings.doctor {
                    DoctorList(result: doctor)
                }
            }

            Section("Display") {
                LabeledContent("Colors", value: colorsText)
                LabeledContent("Time format", value: "24-hour")
                Text("Colors can be changed with `ccs config set display.colors.yellow_from=…`.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private static let notificationToggles: [(key: String, title: String)] = [
        ("limit_warn", "Limit warnings"),
        ("limit_pause", "Sessions paused"),
        ("limit_resume", "Sessions resumed"),
        ("warmup", "Warm-ups"),
        ("errors", "Errors (sign-in required, usage unavailable, invalid config)"),
    ]

    private var colorsText: String {
        let store = settings.store
        guard let yellow = store.int(.root("display.colors.yellow_from")),
              let red = store.int(.root("display.colors.red_from")) else { return "–" }
        return "Green < \(yellow) %, yellow \(yellow)–\(red - 1) %, red ≥ \(red) %"
    }

    private var daemonText: String {
        if let error = settings.daemonError { return error }
        guard let status = settings.daemonStatus else { return "Checking…" }
        if status.responsive == true {
            var parts = ["Running"]
            if let pid = status.daemonPid ?? status.pid { parts.append("pid \(pid)") }
            if let up = status.uptimeS { parts.append("up \(Self.duration(up))") }
            return parts.joined(separator: " · ")
        }
        if status.installed != true { return "Not installed" }
        if status.loaded == true { return "Loaded but not responding" }
        return "Stopped"
    }

    private var daemonColor: Color {
        settings.daemonStatus?.responsive == true ? .primary : .orange
    }

    static func duration(_ seconds: Int) -> String {
        let h = seconds / 3600
        let m = (seconds % 3600) / 60
        if h >= 24 { return "\(h / 24)d \(h % 24)h" }
        if h > 0 { return "\(h)h \(m)m" }
        return "\(m)m"
    }
}

private struct DoctorList: View {
    let result: DoctorResult

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let summary = result.summary {
                Text("\(summary.ok ?? 0) ok · \(summary.warn ?? 0) warnings · \(summary.fail ?? 0) failed")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(Array((result.checks ?? []).enumerated()), id: \.offset) { _, check in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: icon(check.status))
                        .foregroundStyle(color(check.status))
                    VStack(alignment: .leading, spacing: 1) {
                        Text([check.scope, check.message].compactMap { $0 }.joined(separator: ": "))
                        if let fix = check.fix, !fix.isEmpty, check.status != "ok" {
                            Text(fix).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                    }
                }
            }
        }
    }

    private func icon(_ status: String?) -> String {
        switch status {
        case "ok": "checkmark.circle.fill"
        case "warn": "exclamationmark.triangle.fill"
        default: "xmark.octagon.fill"
        }
    }

    private func color(_ status: String?) -> Color {
        switch status {
        case "ok": .green
        case "warn": .orange
        default: .red
        }
    }
}
