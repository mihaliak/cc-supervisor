import AppKit
import SwiftUI

/// The app's icon, name and versions. `large` is the About pane's hero; the compact
/// form heads the General pane.
struct AppIdentityHeader: View {
    @Environment(SettingsController.self) private var settings
    var large = false

    var body: some View {
        HStack(spacing: large ? 18 : 12) {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .interpolation(.high)
                .frame(width: large ? 96 : 44, height: large ? 96 : 44)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: large ? 4 : 1) {
                Text("CC Supervisor")
                    .font(large ? .title.weight(.bold) : .headline)
                if large {
                    Text("Keeps an eye on your Claude Code limits across accounts.")
                        .foregroundStyle(.secondary)
                }
                Text(versionText)
                    .font(large ? .callout : .caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private var versionText: String {
        let app = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let ccs = settings.ccsVersion.map { " · \($0)" } ?? ""
        return "Version \(app)\(ccs)"
    }
}

/// About pane (ADR-0020): identity plus a short description of every feature.
struct AboutSettingsView: View {
    struct Feature: Identifiable {
        let icon: String
        let title: String
        let text: LocalizedStringKey
        var id: String { title }
    }

    private static let features: [Feature] = [
        Feature(icon: "menubar.rectangle", title: "Menu bar",
                text: "Session and weekly usage for every profile: its letter, the percentages and a colored dot."),
        Feature(icon: "rectangle.3.group", title: "Widgets",
                text: "Small, medium and large desktop widgets, one profile each. The small one shows a bar or a gauge."),
        Feature(icon: "terminal", title: "ccs launcher",
                text: "`ccs --work` opens the normal Claude Code with the right account and a usage statusline."),
        Feature(icon: "pause.circle", title: "Supervisor",
                text: "Warns at 80 %, pauses your `ccs` sessions at 90 % (weekly 95 %) and resumes them after the reset."),
        Feature(icon: "gauge.with.dots.needle.67percent", title: "Model and credit limits",
                text: "The Fable weekly limit and extra-usage credits get their own warn and pause rules."),
        Feature(icon: "sunrise", title: "Warm-ups",
                text: "Starts the 5-hour window early (on a schedule, at login or unlock, right after a reset) so more windows fit into your day."),
        Feature(icon: "text.alignleft", title: "Statusline",
                text: "Usage, reset time and pause state in Claude Code's status bar, optionally in plain `claude` too."),
        Feature(icon: "bell.badge", title: "Notifications",
                text: "Limit warnings, pauses, resumes, warm-ups and sign-in problems."),
        Feature(icon: "person.2", title: "Profiles and sign-in",
                text: "One profile per Claude config dir, signed in with Claude Code's own claude.ai login. No passwords or tokens are stored."),
        Feature(icon: "stethoscope", title: "Diagnostics",
                text: "`ccs doctor` checks the whole setup and says how to fix what's wrong."),
    ]

    /// The features in rows of two.
    private static var rows: [[Feature]] {
        stride(from: 0, to: features.count, by: 2).map { Array(features[$0..<min($0 + 2, features.count)]) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                AppIdentityHeader(large: true)
                Divider()
                // A two-column grid: both columns get the same width, every icon and title
                // starts at its column's x, and the two titles of a row share one baseline.
                Grid(alignment: .topLeading, horizontalSpacing: 20, verticalSpacing: 16) {
                    ForEach(Self.rows, id: \.first?.id) { row in
                        GridRow(alignment: .firstTextBaseline) {
                            ForEach(row) { feature in
                                FeatureCell(feature: feature)
                            }
                        }
                    }
                }
                Divider()
                VStack(alignment: .leading, spacing: 4) {
                    Text("The full manual is in `docs/manual` of the cc-supervisor repository.")
                    Text("CCS lettering: Bricolage Grotesque, SIL Open Font License 1.1.")
                    Text("Personal build, signed locally.")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .padding(24)
        }
    }
}

/// One About feature: a fixed-width icon column, then the title and its description.
private struct FeatureCell: View {
    let feature: AboutSettingsView.Feature

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            // Font-sized symbols keep one optical size; the fixed width keeps every title at the same x.
            Image(systemName: feature.icon)
                .font(.system(size: 19))
                .frame(width: 28)
                .foregroundStyle(Color(red: 0.95, green: 0.42, blue: 0.1))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(feature.title).font(.headline)
                Text(feature.text)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
