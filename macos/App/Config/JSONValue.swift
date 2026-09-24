import Foundation

/// One step into a JSON document.
enum PathComponent: Hashable, Sendable {
    case key(String)
    case index(Int)
}

/// A JSON document as a value tree. The Settings UI edits `config.json` through this tree
/// (never through the typed model), so unknown keys always survive a write (ADR-0004).
enum JSONValue: Sendable, Equatable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case int(Int64)
    case double(Double)
    case bool(Bool)
    case null

    // MARK: - Accessors

    var objectValue: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    /// Whole numbers only (JSON `true`/`false` are not numbers, as in the Python validator).
    var intValue: Int? {
        switch self {
        case .int(let i): Int(exactly: i)
        case .double(let d) where d.rounded() == d && abs(d) < 1e15: Int(d)
        default: nil
        }
    }

    var boolValue: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }

    var isNull: Bool { self == .null }

    subscript(key: String) -> JSONValue? {
        objectValue?[key]
    }

    func value(at path: [PathComponent]) -> JSONValue? {
        var current = self
        for component in path {
            switch component {
            case .key(let key):
                guard let next = current.objectValue?[key] else { return nil }
                current = next
            case .index(let index):
                guard let array = current.arrayValue, array.indices.contains(index) else { return nil }
                current = array[index]
            }
        }
        return current
    }

    /// Set (or, with `nil`, remove) the value at `path`, creating intermediate objects for
    /// missing keys. Returns false when the path runs through a non-container or a missing index.
    @discardableResult
    mutating func setValue(_ newValue: JSONValue?, at path: [PathComponent]) -> Bool {
        guard let first = path.first else {
            if let newValue { self = newValue }
            return true
        }
        let rest = Array(path.dropFirst())
        switch first {
        case .key(let key):
            var object: [String: JSONValue]
            switch self {
            case .object(let o): object = o
            case .null: object = [:]
            default: return false
            }
            if rest.isEmpty {
                object[key] = newValue
            } else {
                var child = object[key] ?? .null
                if child == .null, newValue == nil { return true }
                guard child.setValue(newValue, at: rest) else { return false }
                object[key] = child
            }
            self = .object(object)
            return true
        case .index(let index):
            guard var array = arrayValue, array.indices.contains(index) else { return false }
            if rest.isEmpty {
                if let newValue { array[index] = newValue } else { array.remove(at: index) }
            } else {
                guard array[index].setValue(newValue, at: rest) else { return false }
            }
            self = .array(array)
            return true
        }
    }
}

// MARK: - Parsing

enum JSONParseError: Error, Equatable, LocalizedError {
    case unexpectedEnd
    case unexpected(character: Character, offset: Int)
    case invalidNumber(offset: Int)
    case invalidEscape(offset: Int)
    case trailingData(offset: Int)

    var errorDescription: String? {
        switch self {
        case .unexpectedEnd: "unexpected end of JSON"
        case .unexpected(let c, let o): "unexpected '\(c)' at byte \(o)"
        case .invalidNumber(let o): "invalid number at byte \(o)"
        case .invalidEscape(let o): "invalid escape at byte \(o)"
        case .trailingData(let o): "unexpected data after JSON at byte \(o)"
        }
    }
}

extension JSONValue {
    /// Strict RFC 8259 parser. Keeps ints and floats apart by their spelling (like Python's
    /// `json`), so a round trip never turns `92` into `92.0`. Duplicate keys: the last one wins.
    static func parse(_ data: Data) throws -> JSONValue {
        var parser = JSONParser(bytes: [UInt8](data))
        return try parser.parseDocument()
    }

    static func parse(_ text: String) throws -> JSONValue {
        try parse(Data(text.utf8))
    }
}

private struct JSONParser {
    let bytes: [UInt8]
    var pos = 0

    init(bytes: [UInt8]) {
        // Skip a UTF-8 byte order mark.
        if bytes.starts(with: [0xEF, 0xBB, 0xBF]) {
            self.bytes = Array(bytes.dropFirst(3))
        } else {
            self.bytes = bytes
        }
    }

    mutating func parseDocument() throws -> JSONValue {
        skipWhitespace()
        let value = try parseValue()
        skipWhitespace()
        guard pos == bytes.count else { throw JSONParseError.trailingData(offset: pos) }
        return value
    }

    private mutating func skipWhitespace() {
        while pos < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[pos]) { pos += 1 }
    }

    private func peek() throws -> UInt8 {
        guard pos < bytes.count else { throw JSONParseError.unexpectedEnd }
        return bytes[pos]
    }

    private func unexpected() -> JSONParseError {
        guard pos < bytes.count else { return .unexpectedEnd }
        return .unexpected(character: Character(UnicodeScalar(bytes[pos])), offset: pos)
    }

    private mutating func expect(_ literal: String) throws {
        for byte in literal.utf8 {
            guard pos < bytes.count, bytes[pos] == byte else { throw unexpected() }
            pos += 1
        }
    }

    private mutating func parseValue() throws -> JSONValue {
        switch try peek() {
        case UInt8(ascii: "{"): return try parseObject()
        case UInt8(ascii: "["): return try parseArray()
        case UInt8(ascii: "\""): return .string(try parseString())
        case UInt8(ascii: "t"): try expect("true"); return .bool(true)
        case UInt8(ascii: "f"): try expect("false"); return .bool(false)
        case UInt8(ascii: "n"): try expect("null"); return .null
        case UInt8(ascii: "-"), UInt8(ascii: "0")...UInt8(ascii: "9"): return try parseNumber()
        default: throw unexpected()
        }
    }

    private mutating func parseObject() throws -> JSONValue {
        pos += 1
        var object: [String: JSONValue] = [:]
        skipWhitespace()
        if try peek() == UInt8(ascii: "}") {
            pos += 1
            return .object(object)
        }
        while true {
            skipWhitespace()
            guard try peek() == UInt8(ascii: "\"") else { throw unexpected() }
            let key = try parseString()
            skipWhitespace()
            guard try peek() == UInt8(ascii: ":") else { throw unexpected() }
            pos += 1
            skipWhitespace()
            object[key] = try parseValue()
            skipWhitespace()
            let next = try peek()
            pos += 1
            if next == UInt8(ascii: "}") { return .object(object) }
            guard next == UInt8(ascii: ",") else { pos -= 1; throw unexpected() }
        }
    }

    private mutating func parseArray() throws -> JSONValue {
        pos += 1
        var array: [JSONValue] = []
        skipWhitespace()
        if try peek() == UInt8(ascii: "]") {
            pos += 1
            return .array(array)
        }
        while true {
            skipWhitespace()
            array.append(try parseValue())
            skipWhitespace()
            let next = try peek()
            pos += 1
            if next == UInt8(ascii: "]") { return .array(array) }
            guard next == UInt8(ascii: ",") else { pos -= 1; throw unexpected() }
        }
    }

    private mutating func parseHex4() throws -> UInt32 {
        guard pos + 4 <= bytes.count else { throw JSONParseError.unexpectedEnd }
        var value: UInt32 = 0
        for _ in 0..<4 {
            let b = bytes[pos]
            let digit: UInt32
            switch b {
            case UInt8(ascii: "0")...UInt8(ascii: "9"): digit = UInt32(b - UInt8(ascii: "0"))
            case UInt8(ascii: "a")...UInt8(ascii: "f"): digit = UInt32(b - UInt8(ascii: "a") + 10)
            case UInt8(ascii: "A")...UInt8(ascii: "F"): digit = UInt32(b - UInt8(ascii: "A") + 10)
            default: throw JSONParseError.invalidEscape(offset: pos)
            }
            value = value * 16 + digit
            pos += 1
        }
        return value
    }

    private mutating func parseString() throws -> String {
        pos += 1  // opening quote
        var out: [UInt8] = []
        while true {
            guard pos < bytes.count else { throw JSONParseError.unexpectedEnd }
            let b = bytes[pos]
            pos += 1
            switch b {
            case UInt8(ascii: "\""):
                return String(decoding: out, as: UTF8.self)
            case UInt8(ascii: "\\"):
                guard pos < bytes.count else { throw JSONParseError.unexpectedEnd }
                let e = bytes[pos]
                pos += 1
                switch e {
                case UInt8(ascii: "\""): out.append(0x22)
                case UInt8(ascii: "\\"): out.append(0x5C)
                case UInt8(ascii: "/"): out.append(0x2F)
                case UInt8(ascii: "b"): out.append(0x08)
                case UInt8(ascii: "f"): out.append(0x0C)
                case UInt8(ascii: "n"): out.append(0x0A)
                case UInt8(ascii: "r"): out.append(0x0D)
                case UInt8(ascii: "t"): out.append(0x09)
                case UInt8(ascii: "u"):
                    var code = try parseHex4()
                    if (0xD800...0xDBFF).contains(code),
                       pos + 6 <= bytes.count, bytes[pos] == UInt8(ascii: "\\"), bytes[pos + 1] == UInt8(ascii: "u") {
                        let save = pos
                        pos += 2
                        let low = try parseHex4()
                        if (0xDC00...0xDFFF).contains(low) {
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        } else {
                            pos = save
                        }
                    }
                    let scalar = UnicodeScalar(code) ?? "\u{FFFD}"
                    out.append(contentsOf: Array(String(Character(scalar)).utf8))
                default:
                    throw JSONParseError.invalidEscape(offset: pos - 1)
                }
            default:
                guard b >= 0x20 else { throw JSONParseError.unexpected(character: Character(UnicodeScalar(b)), offset: pos - 1) }
                out.append(b)
            }
        }
    }

    private mutating func parseNumber() throws -> JSONValue {
        let start = pos
        var isFloat = false
        if bytes[pos] == UInt8(ascii: "-") { pos += 1 }
        func digits() -> Int {
            var n = 0
            while pos < bytes.count, (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(bytes[pos]) { pos += 1; n += 1 }
            return n
        }
        guard pos < bytes.count else { throw JSONParseError.invalidNumber(offset: start) }
        if bytes[pos] == UInt8(ascii: "0") {
            pos += 1
        } else if digits() == 0 {
            throw JSONParseError.invalidNumber(offset: start)
        }
        if pos < bytes.count, bytes[pos] == UInt8(ascii: ".") {
            isFloat = true
            pos += 1
            guard digits() > 0 else { throw JSONParseError.invalidNumber(offset: start) }
        }
        if pos < bytes.count, bytes[pos] == UInt8(ascii: "e") || bytes[pos] == UInt8(ascii: "E") {
            isFloat = true
            pos += 1
            if pos < bytes.count, bytes[pos] == UInt8(ascii: "+") || bytes[pos] == UInt8(ascii: "-") { pos += 1 }
            guard digits() > 0 else { throw JSONParseError.invalidNumber(offset: start) }
        }
        let text = String(decoding: bytes[start..<pos], as: UTF8.self)
        if !isFloat, let i = Int64(text) { return .int(i) }
        guard let d = Double(text) else { throw JSONParseError.invalidNumber(offset: start) }
        return .double(d)
    }
}

// MARK: - Canonical serialization

extension JSONValue {
    /// The exact bytes Python writes: `json.dumps(obj, ensure_ascii=False, indent=2,
    /// sort_keys=True) + "\n"` (`ccs.fsio.dumps_json`, ADR-0004). Identical output on both
    /// sides means neither writer churns the other's file.
    func canonicalText() -> String {
        var out = ""
        write(into: &out, indent: 0)
        out += "\n"
        return out
    }

    func canonicalData() -> Data {
        Data(canonicalText().utf8)
    }

    private func write(into out: inout String, indent: Int) {
        switch self {
        case .null: out += "null"
        case .bool(let b): out += b ? "true" : "false"
        case .int(let i): out += String(i)
        case .double(let d): out += Self.pythonFloatRepr(d)
        case .string(let s): Self.writeString(s, into: &out)
        case .array(let array):
            guard !array.isEmpty else { out += "[]"; return }
            out += "[\n"
            let pad = String(repeating: " ", count: indent + 2)
            for (i, element) in array.enumerated() {
                out += pad
                element.write(into: &out, indent: indent + 2)
                out += i == array.count - 1 ? "\n" : ",\n"
            }
            out += String(repeating: " ", count: indent) + "]"
        case .object(let object):
            guard !object.isEmpty else { out += "{}"; return }
            out += "{\n"
            let pad = String(repeating: " ", count: indent + 2)
            let keys = object.keys.sorted(by: Self.pythonKeyOrder)
            for (i, key) in keys.enumerated() {
                out += pad
                Self.writeString(key, into: &out)
                out += ": "
                object[key]!.write(into: &out, indent: indent + 2)
                out += i == keys.count - 1 ? "\n" : ",\n"
            }
            out += String(repeating: " ", count: indent) + "}"
        }
    }

    /// Python sorts `str` keys by code point.
    static func pythonKeyOrder(_ a: String, _ b: String) -> Bool {
        a.unicodeScalars.lexicographicallyPrecedes(b.unicodeScalars)
    }

    /// Python's `json` string escaping with `ensure_ascii=False`: only `"`, `\` and
    /// control characters below U+0020 are escaped (lowercase `\u00xx`).
    static func writeString(_ s: String, into out: inout String) {
        out += "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case "\u{08}": out += "\\b"
            case "\u{0C}": out += "\\f"
            default:
                if scalar.value < 0x20 {
                    out += "\\u" + String(format: "%04x", scalar.value)
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        out += "\""
    }

    /// Python's `repr(float)`: shortest round-trip digits; scientific notation when the
    /// decimal exponent is < -4 or >= 16 (`1e-05`, `1e+16`), otherwise fixed with `.0`.
    static func pythonFloatRepr(_ value: Double) -> String {
        if value.isNaN { return "NaN" }
        if value.isInfinite { return value < 0 ? "-Infinity" : "Infinity" }
        let negative = value.sign == .minus
        let text = "\(abs(value))"  // Swift: shortest round-trip digits
        var mantissa = text
        var exponent = 0
        if let e = text.firstIndex(where: { $0 == "e" || $0 == "E" }) {
            mantissa = String(text[..<e])
            exponent = Int(text[text.index(after: e)...]) ?? 0
        }
        let parts = mantissa.split(separator: ".", omittingEmptySubsequences: false)
        let intPart = String(parts[0])
        let fracPart = parts.count > 1 ? String(parts[1]) : ""
        var digits = Array(intPart + fracPart)
        var decpt = intPart.count + exponent
        while let first = digits.first, first == "0" {
            digits.removeFirst()
            decpt -= 1
        }
        while let last = digits.last, last == "0" { digits.removeLast() }
        let sign = negative ? "-" : ""
        if digits.isEmpty { return sign + "0.0" }
        let d = String(digits)
        if decpt > -4 && decpt <= 16 {
            if decpt <= 0 { return sign + "0." + String(repeating: "0", count: -decpt) + d }
            if decpt >= d.count { return sign + d + String(repeating: "0", count: decpt - d.count) + ".0" }
            let split = d.index(d.startIndex, offsetBy: decpt)
            return sign + d[..<split] + "." + d[split...]
        }
        let exp = decpt - 1
        let head = String(d.first!) + (d.count > 1 ? "." + d.dropFirst() : "")
        let expText = (exp < 0 ? "-" : "+") + (abs(exp) < 10 ? "0" : "") + String(abs(exp))
        return sign + head + "e" + expText
    }
}
