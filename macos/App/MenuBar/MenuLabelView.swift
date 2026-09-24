import AppKit
import SwiftUI

/// Content of the menu bar label (ADR-0011):
/// - `emoji_percent`: `💼 45%  🏠 12%`, each percent in its session level color
/// - `icon_only`: a gauge tinted by the worst level across profiles
struct MenuLabelContent: View {
    let snapshot: WidgetSnapshot?
    let mode: MenuBarMode

    var body: some View {
        switch mode {
        case .iconOnly:
            gauge(level: snapshot?.worstLevel)
        case .emojiPercent:
            if let profiles = snapshot?.profiles, !profiles.isEmpty {
                HStack(spacing: 6) {
                    ForEach(profiles) { profile in
                        HStack(spacing: 2) {
                            Text(profile.emoji)
                            Text(Self.percentText(profile))
                                .foregroundStyle(Color(nsColor: Self.labelColor(profile.sessionRow?.level)))
                        }
                    }
                }
                .font(.system(size: 13, weight: .medium).monospacedDigit())
            } else {
                gauge(level: nil)
            }
        }
    }

    private func gauge(level: Level?) -> some View {
        Image(systemName: "gauge.with.dots.needle.50percent")
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(Color(nsColor: Self.labelColor(level)))
    }

    nonisolated static func percentText(_ profile: ProfileSnapshot) -> String {
        guard let row = profile.sessionRow, profile.status != .noData else { return "?%" }
        return "\(row.percent)%"
    }

    /// Gray stays readable on both light and dark menu bars.
    nonisolated static func labelColor(_ level: Level?) -> NSColor {
        level == nil ? .systemGray : LevelColor.nsColor(level)
    }
}

/// The always-rendered menu bar label. Status items flatten colored text to a
/// template, so the content is rendered to a non-template image. It also opens
/// Settings on request (it is the one view that is always alive).
struct MenuLabelView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    @Environment(\.displayScale) private var displayScale

    var body: some View {
        Image(nsImage: renderedLabel())
            .onChange(of: model.settingsRequest) { _, _ in
                NSApp.activate()
                openSettings()
            }
    }

    @MainActor
    private func renderedLabel() -> NSImage {
        let renderer = ImageRenderer(content: MenuLabelContent(snapshot: model.snapshot, mode: model.config.menuBarMode))
        renderer.scale = max(displayScale, 2)
        guard let image = renderer.nsImage else {
            let fallback = NSImage(systemSymbolName: "gauge.with.dots.needle.50percent", accessibilityDescription: "CC Supervisor")
            return fallback ?? NSImage()
        }
        image.isTemplate = false
        image.accessibilityDescription = "CC Supervisor"
        return image
    }
}
