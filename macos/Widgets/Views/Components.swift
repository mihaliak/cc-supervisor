import SwiftUI
import WidgetKit

/// `emoji name` plus the paused badge.
struct HeaderView: View {
    let display: WidgetDisplay
    var compact = false

    var body: some View {
        HStack(spacing: 4) {
            Text(display.title)
                .font(compact ? .caption.weight(.semibold) : .headline)
                .lineLimit(1)
            Spacer(minLength: 4)
            if display.isPaused {
                Text(compact ? "⏸" : WidgetDisplay.pausedBadge)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
                    .accessibilityLabel("Paused")
            }
        }
    }
}

/// Capsule bar in the level color (level computed by Python, ADR-0001).
struct WidgetUsageBar: View {
    let percent: Int
    let level: Level?
    var height: CGFloat = 6

    var body: some View {
        GeometryReader { geo in
            let fraction = CGFloat(min(max(percent, 0), 100)) / 100
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.25))
                Capsule()
                    .fill(LevelColor.color(level))
                    .frame(width: percent > 0 ? max(geo.size.width * fraction, height) : 0)
                    .widgetAccentable()
            }
        }
        .frame(height: height)
        .accessibilityLabel("\(percent) percent")
    }
}

/// Medium row: label | percent | bar | reset (or extra-usage detail), one line.
struct CompactUsageRowView: View {
    let row: WidgetRowDisplay

    var body: some View {
        HStack(spacing: 6) {
            Text(row.label)
                .font(.caption)
                .lineLimit(1)
                .frame(width: 64, alignment: .leading)
            Text(row.percentText)
                .font(.caption.monospacedDigit().weight(.semibold))
                .foregroundStyle(LevelColor.color(row.level))
                .frame(width: 36, alignment: .trailing)
            WidgetUsageBar(percent: row.percent, level: row.level, height: 5)
                .frame(width: 56)
            Text(row.trailing ?? "")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer(minLength: 0)
        }
    }
}

/// Large row: label + percent, full-width bar, reset below.
struct StackedUsageRowView: View {
    let row: WidgetRowDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(row.label).font(.callout)
                Spacer(minLength: 6)
                Text(row.percentText)
                    .font(.callout.monospacedDigit().weight(.semibold))
                    .foregroundStyle(LevelColor.color(row.level))
            }
            WidgetUsageBar(percent: row.percent, level: row.level)
            if let trailing = row.trailing {
                Text(trailing)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}

/// Small footer `W 50% · Sat 08:00`, percent in level color.
struct WeeklyLineView: View {
    let line: WidgetCompactLine

    var body: some View {
        HStack(spacing: 0) {
            Text("\(line.prefix) ")
            Text(line.percentText)
                .foregroundStyle(LevelColor.color(line.level))
                .fontWeight(.semibold)
            if let trailing = line.trailing {
                Text(" · \(trailing)")
            }
        }
        .font(.caption2.monospacedDigit())
        .lineLimit(1)
        .minimumScaleFactor(0.7)
    }
}

/// Message for not configured / sign-in / no-data states.
struct StateMessageView: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let message = display.message {
                Text(message)
                    .font(.callout.weight(.semibold))
                    .lineLimit(2)
            }
            switch display.state {
            case .needsSignIn:
                Text("Click to open Settings and sign in")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            case .notConfigured:
                if let footer = display.footer {
                    Text(footer).font(.caption2).foregroundStyle(.secondary)
                }
            default:
                EmptyView()
            }
        }
    }
}

/// `Supervisor offline` / `updated 14m ago`.
struct FooterView: View {
    let display: WidgetDisplay

    var body: some View {
        if display.state == .offline || display.state == .stale, let footer = display.footer {
            Text(footer)
                .font(.caption2)
                .foregroundStyle(display.state == .offline ? .orange : .secondary)
                .lineLimit(1)
        }
    }
}
