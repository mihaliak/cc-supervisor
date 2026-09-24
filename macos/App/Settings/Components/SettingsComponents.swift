import AppKit
import SwiftUI

// MARK: - Time of day

/// `"HH:MM"` config strings ↔ `Date` for `DatePicker(.hourAndMinute)` (presentation only).
/// Uses a fixed reference day (1 Jan 2001) so a daylight-saving gap on "today" (e.g. 02:30
/// on a spring-forward date) can't shift the stored time.
enum TimeOfDay {
    static func parse(_ text: String) -> (hour: Int, minute: Int)? {
        let parts = text.split(separator: ":")
        guard parts.count == 2, parts[0].count == 2, parts[1].count == 2,
              let h = Int(parts[0]), let m = Int(parts[1]),
              (0...23).contains(h), (0...59).contains(m) else { return nil }
        return (h, m)
    }

    static func date(from text: String, calendar: Calendar = .current) -> Date? {
        guard let (h, m) = parse(text) else { return nil }
        return calendar.date(from: DateComponents(year: 2001, month: 1, day: 1, hour: h, minute: m))
    }

    static func string(from date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }
}

// MARK: - Weekdays

enum Weekdays {
    /// Config spelling, Monday first (ADR-0004).
    static let all = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    static func label(_ day: String) -> String {
        day.prefix(1).uppercased() + day.dropFirst()
    }

    /// Toggle `day`, keeping week order and no duplicates.
    static func toggled(_ days: [String], _ day: String) -> [String] {
        var set = Set(days)
        if set.contains(day) { set.remove(day) } else { set.insert(day) }
        return all.filter(set.contains)
    }
}

struct WeekdayChips: View {
    let selected: [String]
    let onChange: ([String]) -> Void

    var body: some View {
        HStack(spacing: 4) {
            ForEach(Weekdays.all, id: \.self) { day in
                let on = selected.contains(day)
                Button(Weekdays.label(day)) {
                    onChange(Weekdays.toggled(selected, day))
                }
                .buttonStyle(.bordered)
                .tint(on ? .accentColor : .secondary)
                .fontWeight(on ? .semibold : .regular)
                .controlSize(.small)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
    }
}

// MARK: - Statusline preview

/// `ccs statusline preview` segments → an `AttributedString` (colors only; text is
/// rendered by Python, ADR-0001).
enum SegmentsPreview {
    static func color(for name: String?) -> Color? {
        switch name {
        case "green": LevelColor.color(.green)
        case "yellow": LevelColor.color(.yellow)
        case "red": LevelColor.color(.red)
        case "gray": .gray
        default: nil
        }
    }

    static func attributed(_ segments: [StatuslinePreviewResult.Segment]) -> AttributedString {
        var out = AttributedString()
        for segment in segments {
            var part = AttributedString(segment.text)
            if let color = color(for: segment.color) { part.foregroundColor = color }
            out += part
        }
        return out
    }
}

// MARK: - Fields

/// Inline validation messages from `ccs config validate` under a field.
struct IssueText: View {
    let messages: [String]

    var body: some View {
        if !messages.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(messages, id: \.self) { message in
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .labelStyle(.titleAndIcon)
                }
            }
            .font(.caption)
            .foregroundStyle(.red)
        }
    }
}

/// A 1–100 % threshold with a stepper.
struct PercentStepper: View {
    let title: String
    @Binding var value: Int
    var range: ClosedRange<Int> = 1...100

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                TextField(title, value: $value, format: .number)
                    .labelsHidden()
                    .multilineTextAlignment(.trailing)
                    .frame(width: 48)
                Text("%").foregroundStyle(.secondary)
                Stepper(title, value: $value, in: range)
                    .labelsHidden()
            }
        }
    }
}

/// Text field for an emoji plus a button opening the system character palette.
struct EmojiField: View {
    let title: String
    @Binding var text: String
    @FocusState private var focused: Bool

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                TextField(title, text: $text)
                    .labelsHidden()
                    .focused($focused)
                    .frame(width: 64)
                Button("😀") {
                    focused = true
                    DispatchQueue.main.async { NSApp.orderFrontCharacterPalette(nil) }
                }
                .help("Open the emoji palette")
            }
        }
    }
}

/// A text field that writes its binding only on Return or when it loses focus, so a
/// half-typed value (e.g. a path) is never saved.
struct CommitTextField: View {
    let title: String
    @Binding var text: String
    var prompt = ""
    @State private var draft = ""
    @FocusState private var focused: Bool

    var body: some View {
        TextField(title, text: $draft, prompt: Text(prompt))
            .focused($focused)
            .onSubmit(commit)
            .onAppear { draft = text }
            .onChange(of: text) { _, newValue in
                if !focused { draft = newValue }
            }
            .onChange(of: focused) { _, isFocused in
                if !isFocused { commit() }
            }
    }

    private func commit() {
        if draft != text { text = draft }
    }
}

/// A path field plus "Choose…" (`NSOpenPanel` for directories, hidden files shown). Typed
/// paths are saved on Return or when the field loses focus (see `CommitTextField`).
struct DirectoryField: View {
    let title: String
    @Binding var path: String
    var placeholder = ""

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                CommitTextField(title: title, text: $path, prompt: placeholder)
                    .labelsHidden()
                    .font(.body.monospaced())
                Button("Choose…") { choose() }
            }
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true
        panel.canCreateDirectories = false
        let current = ConfigReader.expandTilde(path, home: StateLocation.realHome())
        panel.directoryURL = path.isEmpty ? StateLocation.realHome() : URL(fileURLWithPath: current)
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        path = Self.abbreviate(url.path)
    }

    /// `/Users/me/.claude-work` → `~/.claude-work` (stored as typed, ADR-0004).
    static func abbreviate(_ path: String, home: URL = StateLocation.realHome()) -> String {
        let h = home.path
        if path == h { return "~" }
        if path.hasPrefix(h + "/") { return "~" + path.dropFirst(h.count) }
        return path
    }
}

/// A red or orange banner with an icon.
struct SettingsBanner: View {
    let text: String
    var systemImage = "exclamationmark.triangle.fill"
    var tint: Color = .red

    var body: some View {
        Label(text, systemImage: systemImage)
            .font(.callout)
            .foregroundStyle(tint)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 6))
    }
}

/// The result line under a group of buttons.
struct MessageLine: View {
    let message: SettingsMessage?

    var body: some View {
        if let message {
            Text(message.text)
                .font(.caption)
                .foregroundStyle(message.isError ? Color.red : Color.secondary)
                .textSelection(.enabled)
        }
    }
}
