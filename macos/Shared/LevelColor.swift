import AppKit
import SwiftUI

/// Maps a Python-computed level to a color (ADR-0009). No threshold math here (ADR-0001).
public enum LevelColor {
    public static func color(_ level: Level?) -> Color {
        switch level {
        case .green: .green
        case .yellow: .yellow
        case .red: .red
        case nil: .secondary
        }
    }

    public static func nsColor(_ level: Level?) -> NSColor {
        switch level {
        case .green: .systemGreen
        case .yellow: .systemYellow
        case .red: .systemRed
        case nil: .secondaryLabelColor
        }
    }
}
