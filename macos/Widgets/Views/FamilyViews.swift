import SwiftUI
import WidgetKit

/// Small: header, big percent + bar (or gauge) + reset for the featured row (the session,
/// else the first row shown, captioned with its label), weekly footer.
struct SmallWidgetView: View {
    let display: WidgetDisplay
    var style: SmallWidgetStyle = .bar

    var body: some View {
        VStack(alignment: style == .gauge ? .center : .leading, spacing: 4) {
            HeaderView(display: display, compact: true, centered: style == .gauge)
            Spacer(minLength: 0)
            if display.showsValues, let row = display.smallRow, style == .gauge {
                VStack(spacing: 2) {
                    SmallCaption(text: display.smallCaption)
                    UsageGaugeView(
                        percent: row.percent, level: row.level, percentText: row.percentText,
                        label: display.smallCaption ?? "Session"
                    )
                    .frame(maxWidth: 118)
                    SmallResetLine(text: row.trailing)
                }
                .frame(maxWidth: .infinity)
                .opacity(display.dimmed ? 0.5 : 1)
                Spacer(minLength: 0)
                if let weekly = display.smallWeeklyLine {
                    WeeklyLineView(line: weekly)
                        .frame(maxWidth: .infinity)
                        .opacity(display.dimmed ? 0.5 : 1)
                }
            } else if display.showsValues, let row = display.smallRow {
                VStack(alignment: .leading, spacing: 4) {
                    SmallCaption(text: display.smallCaption)
                    Text(row.percentText)
                        .font(.system(size: 30, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(LevelColor.color(row.level))
                        .minimumScaleFactor(0.6)
                    WidgetUsageBar(percent: row.percent, level: row.level)
                    SmallResetLine(text: row.trailing)
                }
                .opacity(display.dimmed ? 0.5 : 1)
                Spacer(minLength: 0)
                if let weekly = display.smallWeeklyLine {
                    WeeklyLineView(line: weekly)
                        .opacity(display.dimmed ? 0.5 : 1)
                }
            } else {
                StateMessageView(display: display)
                Spacer(minLength: 0)
            }
            FooterView(display: display)
        }
    }
}

/// Medium: header + up to four one-line rows (drop order: extra, model, weekly).
struct MediumWidgetView: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HeaderView(display: display)
            if display.showsValues {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(display.rows(for: .medium)) { row in
                        CompactUsageRowView(row: row)
                    }
                }
                .opacity(display.dimmed ? 0.5 : 1)
            } else {
                StateMessageView(display: display)
            }
            Spacer(minLength: 0)
            FooterView(display: display)
        }
    }
}

/// Large: medium rows (stacked) + supervisor section.
struct LargeWidgetView: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HeaderView(display: display)
            if let resume = display.resumeText {
                Text(resume)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if display.showsValues {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(display.rows(for: .large)) { row in
                        StackedUsageRowView(row: row)
                    }
                }
                .opacity(display.dimmed ? 0.5 : 1)
            } else {
                StateMessageView(display: display)
            }
            Spacer(minLength: 0)
            if display.state != .notConfigured {
                Divider()
                VStack(alignment: .leading, spacing: 2) {
                    if let line = display.supervisorLine {
                        Text(line)
                    }
                    if let warmup = display.nextWarmupText {
                        Text(warmup)
                    }
                    if let updated = display.updatedText {
                        Text(updated)
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
            FooterView(display: display)
        }
    }
}
