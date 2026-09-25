import SwiftUI

/// ANSI (SGR) text → an attributed string, drawn in a dark terminal window.
enum ANSI {
    /// A dark theme's 16 colors (black, red, green, yellow, blue, magenta, cyan, white, then bright).
    static let palette: [Color] = [
        rgb(0x2E, 0x34, 0x40), rgb(0xF2, 0x6D, 0x6D), rgb(0x7E, 0xD3, 0x7A), rgb(0xF2, 0xC9, 0x4C),
        rgb(0x6C, 0xA8, 0xF7), rgb(0xC6, 0x8C, 0xF2), rgb(0x5F, 0xD3, 0xD3), rgb(0xD8, 0xDE, 0xE9),
        rgb(0x6B, 0x74, 0x86), rgb(0xFF, 0x8A, 0x8A), rgb(0x9C, 0xE6, 0x98), rgb(0xFF, 0xDD, 0x70),
        rgb(0x8F, 0xBF, 0xFF), rgb(0xDB, 0xAA, 0xFF), rgb(0x86, 0xE6, 0xE6), rgb(0xFF, 0xFF, 0xFF),
    ]
    static let foreground = rgb(0xD8, 0xDE, 0xE9)
    static let background = rgb(0x1C, 0x1F, 0x26)

    static func rgb(_ r: Int, _ g: Int, _ b: Int) -> Color {
        Color(red: Double(r) / 255, green: Double(g) / 255, blue: Double(b) / 255)
    }

    /// xterm 256-color index → color.
    static func color256(_ n: Int) -> Color {
        if n < 16 { return palette[n] }
        if n < 232 {
            let i = n - 16
            let steps = [0, 95, 135, 175, 215, 255]
            return rgb(steps[i / 36], steps[(i / 6) % 6], steps[i % 6])
        }
        let level = 8 + (n - 232) * 10
        return rgb(level, level, level)
    }

    struct Style {
        var fg: Color?
        var bg: Color?
        var bold = false
        var dim = false
        var italic = false
        var underline = false
    }

    static func attributed(_ text: String, fontSize: CGFloat) -> AttributedString {
        var out = AttributedString()
        var style = Style()
        var chunk = ""

        func flush() {
            guard !chunk.isEmpty else { return }
            var part = AttributedString(chunk)
            let weight: Font.Weight = style.bold ? .bold : .regular
            var font = Font.custom("Menlo", size: fontSize).weight(weight)
            if style.italic { font = font.italic() }
            part.font = font
            var fg = style.fg ?? foreground
            if style.dim { fg = fg.opacity(0.6) }
            part.foregroundColor = fg
            if let bg = style.bg { part.backgroundColor = bg }
            if style.underline { part.underlineStyle = .single }
            out += part
            chunk = ""
        }

        var chars = Array(text.unicodeScalars)[...]
        while let c = chars.first {
            chars = chars.dropFirst()
            guard c == "\u{1B}" else {
                chunk.unicodeScalars.append(c)
                continue
            }
            guard chars.first == "[" else {
                // OSC and other escapes: skip to the terminator.
                while let next = chars.first, next != "\u{07}", next != "\\" { chars = chars.dropFirst() }
                chars = chars.dropFirst()
                continue
            }
            chars = chars.dropFirst()
            var params = ""
            while let next = chars.first, !(0x40...0x7E).contains(next.value) {
                params.unicodeScalars.append(next)
                chars = chars.dropFirst()
            }
            let final = chars.first
            chars = chars.dropFirst()
            guard final == "m" else { continue }
            flush()
            apply(params, to: &style)
        }
        flush()
        return out
    }

    static func apply(_ params: String, to style: inout Style) {
        var codes = params.split(separator: ";", omittingEmptySubsequences: false).map { Int($0) ?? 0 }[...]
        if codes.isEmpty { codes = [0] }
        while let code = codes.first {
            codes = codes.dropFirst()
            switch code {
            case 0: style = Style()
            case 1: style.bold = true
            case 2: style.dim = true
            case 3: style.italic = true
            case 4: style.underline = true
            case 22: style.bold = false; style.dim = false
            case 23: style.italic = false
            case 24: style.underline = false
            case 30...37: style.fg = palette[code - 30]
            case 39: style.fg = nil
            case 40...47: style.bg = palette[code - 40]
            case 49: style.bg = nil
            case 90...97: style.fg = palette[code - 90 + 8]
            case 100...107: style.bg = palette[code - 100 + 8]
            case 38, 48:
                var color: Color?
                if codes.first == 5, codes.count >= 2 {
                    color = color256(codes[codes.startIndex + 1])
                    codes = codes.dropFirst(2)
                } else if codes.first == 2, codes.count >= 4 {
                    let i = codes.startIndex
                    color = rgb(codes[i + 1], codes[i + 2], codes[i + 3])
                    codes = codes.dropFirst(4)
                }
                if code == 38 { style.fg = color } else { style.bg = color }
            default: break
            }
        }
    }
}

/// A terminal window: prompt + command, then the captured output.
struct TerminalWindow: View {
    let command: String
    let output: String
    var title: String = "zsh"
    static let fontSize: CGFloat = 13

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack {
                Text(title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(Color.white.opacity(0.55))
                HStack {
                    TrafficLights()
                    Spacer()
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color(red: 0.16, green: 0.18, blue: 0.22))
            VStack(alignment: .leading, spacing: 2) {
                Text(ANSI.attributed("\u{1B}[32m❯\u{1B}[0m \u{1B}[1m\(command)\u{1B}[0m", fontSize: Self.fontSize))
                Text(ANSI.attributed(output, fontSize: Self.fontSize))
                    .lineSpacing(2)
            }
            .fixedSize()
            .padding(.horizontal, 18)
            .padding(.top, 12)
            .padding(.bottom, 16)
        }
        .background(ANSI.background)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(0.35), radius: 22, y: 10)
        .fixedSize()
    }
}
