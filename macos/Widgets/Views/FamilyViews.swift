import SwiftUI
import WidgetKit

/// Small: header, big session percent + bar + reset, weekly footer.
struct SmallWidgetView: View {
    let display: WidgetDisplay

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HeaderView(display: display, compact: true)
            Spacer(minLength: 0)
            if display.showsValues, let session = display.session {
                VStack(alignment: .leading, spacing: 4) {
                    Text(session.percentText)
                        .font(.system(size: 30, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(LevelColor.color(session.level))
                        .minimumScaleFactor(0.6)
                    WidgetUsageBar(percent: session.percent, level: session.level)
                    if let trailing = session.trailing {
                        Text(trailing)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                    }
                }
                .opacity(display.dimmed ? 0.5 : 1)
                Spacer(minLength: 0)
                if let weekly = display.weeklyLine {
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
