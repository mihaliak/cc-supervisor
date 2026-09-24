import SwiftUI

/// One limit row: label, percent, capsule bar in level color, `abs · rel` reset
/// (or the preformatted `detail` for extra usage).
struct UsageRowView: View {
    let row: UsageRow
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(row.label)
                    .font(.callout)
                Spacer(minLength: 8)
                Text("\(row.percent)%")
                    .font(.callout.monospacedDigit().weight(.semibold))
                    .foregroundStyle(LevelColor.color(row.level))
            }
            UsageBar(percent: row.percent, level: row.level)
            if let trailing = Self.trailingText(row, now: now) {
                Text(trailing)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    nonisolated static func trailingText(_ row: UsageRow, now: Date) -> String? {
        if row.kind == .extraUsage, let detail = row.detail, !detail.isEmpty {
            return detail
        }
        if let resets = row.resetsAt {
            return TimeFormat.compact(resets, now: now)
        }
        return row.detail
    }
}

struct UsageBar: View {
    let percent: Int
    let level: Level?

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.2))
                Capsule()
                    .fill(LevelColor.color(level))
                    .frame(width: geo.size.width * CGFloat(min(max(percent, 0), 100)) / 100)
            }
        }
        .frame(height: 6)
        .accessibilityLabel("\(percent) percent")
    }
}
