import AppKit
import SwiftUI

/// Content of the menu bar label (ADR-0018):
/// - `letter_percent`: `P 2% ● 63% ●   W 24% ● 99% ●`. Letter, session %, session
///   dot, weekly %, weekly dot per profile; only the dots carry the level color.
/// - `icon_only`: a gauge tinted by the worst level across profiles
struct MenuLabelContent: View {
    let snapshot: WidgetSnapshot?
    let mode: MenuBarMode
    /// Letters and percents follow the menu bar's appearance (the label is an image).
    var textColor: Color = .primary

    static let fontSize: CGFloat = 11
    static let dotSize: CGFloat = 6

    var body: some View {
        switch mode {
        case .iconOnly:
            gauge(level: snapshot?.worstLevel)
        case .letterPercent:
            if let profiles = snapshot?.profiles, !profiles.isEmpty {
                HStack(spacing: 10) {
                    ForEach(profiles) { profile in
                        HStack(spacing: 3) {
                            Text(profile.initial)
                            Text(Self.percentText(profile.sessionRow, profile: profile))
                            dot(Self.dotLevel(profile.sessionRow, profile: profile))
                            Text(Self.percentText(profile.weeklyRow, profile: profile))
                            dot(Self.dotLevel(profile.weeklyRow, profile: profile))
                        }
                    }
                }
                .font(.system(size: Self.fontSize, weight: .medium).monospacedDigit())
                .foregroundStyle(textColor)
            } else {
                gauge(level: nil)
            }
        }
    }

    private func dot(_ level: Level?) -> some View {
        Circle()
            .fill(Color(nsColor: Self.labelColor(level)))
            .frame(width: Self.dotSize, height: Self.dotSize)
    }

    private func gauge(level: Level?) -> some View {
        Image(systemName: "gauge.with.dots.needle.50percent")
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(Color(nsColor: Self.labelColor(level)))
    }

    nonisolated static func percentText(_ row: UsageRow?, profile: ProfileSnapshot) -> String {
        guard let row, profile.status != .noData else { return "?%" }
        return "\(row.percent)%"
    }

    /// No data → gray dot.
    nonisolated static func dotLevel(_ row: UsageRow?, profile: ProfileSnapshot) -> Level? {
        guard let row, profile.status != .noData else { return nil }
        return row.level
    }

    /// Gray stays readable on both light and dark menu bars.
    nonisolated static func labelColor(_ level: Level?) -> NSColor {
        level == nil ? .systemGray : LevelColor.nsColor(level)
    }

    /// VoiceOver text: "Personal: session 2%, weekly 63%. Work: …".
    nonisolated static func accessibilityText(_ snapshot: WidgetSnapshot?) -> String {
        guard let profiles = snapshot?.profiles, !profiles.isEmpty else { return "CC Supervisor" }
        return profiles.map { profile in
            "\(profile.name): session \(percentText(profile.sessionRow, profile: profile)), "
                + "weekly \(percentText(profile.weeklyRow, profile: profile))"
        }.joined(separator: ". ")
    }
}

/// The always-rendered menu bar label. Status items flatten colored text to a
/// template, so the content is rendered to a non-template image; its text color
/// is picked from the menu bar's color scheme. It also opens Settings on request
/// (it is the one view that is always alive).
struct MenuLabelView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    @Environment(\.displayScale) private var displayScale
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        Image(nsImage: renderedLabel())
            .onChange(of: model.settingsRequest) { _, _ in
                NSApp.activate()
                openSettings()
            }
    }

    @MainActor
    private func renderedLabel() -> NSImage {
        let textColor: Color = colorScheme == .dark ? .white : .black
        let content = MenuLabelContent(snapshot: model.snapshot, mode: model.config.menuBarMode, textColor: textColor)
            .environment(\.colorScheme, colorScheme)
        let renderer = ImageRenderer(content: content)
        renderer.scale = max(displayScale, 2)
        guard let image = renderer.nsImage else {
            let fallback = NSImage(systemSymbolName: "gauge.with.dots.needle.50percent", accessibilityDescription: "CC Supervisor")
            return fallback ?? NSImage()
        }
        image.isTemplate = false
        image.accessibilityDescription = MenuLabelContent.accessibilityText(model.snapshot)
        return image
    }
}
