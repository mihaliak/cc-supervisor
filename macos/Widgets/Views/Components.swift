import SwiftUI
import WidgetKit

/// `emoji name` plus the paused badge.
struct HeaderView: View {
    let display: WidgetDisplay
    var compact = false
    /// Small widget in Gauge style: name (and the paused mark next to it) centered.
    var centered = false

    var body: some View {
        HStack(spacing: 4) {
            if centered { Spacer(minLength: 0) }
            Text(display.title)
                .font(compact ? .caption.weight(.semibold) : .headline)
                .lineLimit(1)
            if !centered { Spacer(minLength: 4) }
            if display.isPaused {
                Text(compact ? "⏸" : WidgetDisplay.pausedBadge)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
                    .accessibilityLabel("Paused")
            }
            if centered { Spacer(minLength: 0) }
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

/// Small widget: the reset time under the session percent. Without one the row stays, empty,
/// so a small widget keeps its layout (the gauge doesn't grow into the gap).
struct SmallResetLine: View {
    let text: String?

    var body: some View {
        let shown = text.flatMap { $0.isEmpty ? nil : $0 }
        Text(shown ?? "0")
            .font(.caption2)
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            .opacity(shown == nil ? 0 : 1)
            .accessibilityHidden(shown == nil)
    }
}

/// Small widget: what the featured row is, when it isn't the session row.
struct SmallCaption: View {
    let text: String?

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
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

/// Half-circle usage gauge (small widget "Gauge" style, like the app icon): track, a
/// progress arc and needle in the session's level color, percent under the hub.
struct UsageGaugeView: View {
    let percent: Int
    let level: Level?
    let percentText: String
    /// What the gauge measures, for VoiceOver.
    var label = "Session"

    var body: some View {
        let color = LevelColor.color(level)
        VStack(spacing: 0) {
            GeometryReader { geo in
                let width = geo.size.width
                let line = width * 0.11
                let radius = (width - line) / 2
                let center = CGPoint(x: width / 2, y: radius + line / 2)
                let fraction = GaugeGeometry.fraction(percent: percent)
                ZStack {
                    HalfArc(center: center, radius: radius)
                        .stroke(color.opacity(0.22), style: StrokeStyle(lineWidth: line, lineCap: .round))
                    HalfArc(center: center, radius: radius)
                        .trim(from: 0, to: fraction)
                        .stroke(color, style: StrokeStyle(lineWidth: line, lineCap: .round))
                        .widgetAccentable()
                    Path { p in
                        p.move(to: center)
                        p.addLine(to: GaugeGeometry.needleTip(center: center, length: radius * 0.72, percent: percent))
                    }
                    .stroke(Color.primary, style: StrokeStyle(lineWidth: line * 0.45, lineCap: .round))
                    Circle()
                        .fill(Color.primary)
                        .frame(width: line * 1.1, height: line * 1.1)
                        .position(center)
                }
            }
            .aspectRatio(1.72, contentMode: .fit)
            Text(percentText)
                .font(.system(size: 22, weight: .bold, design: .rounded).monospacedDigit())
                .foregroundStyle(color)
                .minimumScaleFactor(0.6)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label) \(percent) percent")
    }
}

/// The gauge's half circle, from the left end over the top to the right end.
struct HalfArc: Shape {
    let center: CGPoint
    let radius: CGFloat

    func path(in rect: CGRect) -> Path {
        Path { p in
            p.addArc(
                center: center, radius: radius,
                startAngle: .degrees(GaugeGeometry.startDegrees), endAngle: .degrees(GaugeGeometry.endDegrees),
                clockwise: false
            )
        }
    }
}
