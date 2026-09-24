import Foundation

/// Where a setting lives in `config.json`: a top-level key path, or a key path inside the
/// profile with a given id. Profiles are addressed by id (not index) so a pending edit still
/// lands on the right profile after another writer reordered the list.
struct FieldPath: Hashable, Sendable, CustomStringConvertible {
    var profileID: String?
    /// Dotted key path, with `[n]` for array indices: `limits.session.pause`,
    /// `warmup.triggers.schedule[0].time`. Empty = the profile (or document) itself.
    var dotted: String

    static func root(_ dotted: String) -> FieldPath {
        FieldPath(profileID: nil, dotted: dotted)
    }

    static func profile(_ id: String, _ dotted: String) -> FieldPath {
        FieldPath(profileID: id, dotted: dotted)
    }

    var components: [PathComponent] { Self.parse(dotted) }

    var description: String {
        guard let profileID else { return dotted }
        return dotted.isEmpty ? "profiles[id=\(profileID)]" : "profiles[id=\(profileID)].\(dotted)"
    }

    /// True when `self` is `other` or lies inside it (`limits.session.pause` is inside `limits`).
    func isWithin(_ other: FieldPath) -> Bool {
        guard profileID == other.profileID else { return false }
        if other.dotted.isEmpty || dotted == other.dotted { return true }
        return dotted.hasPrefix(other.dotted + ".") || dotted.hasPrefix(other.dotted + "[")
    }

    /// The absolute document path in `raw`, resolving the profile id to its current index.
    func resolved(in raw: JSONValue) -> [PathComponent]? {
        guard let profileID else { return components }
        guard let index = Self.profileIndex(of: profileID, in: raw) else { return nil }
        return [.key("profiles"), .index(index)] + components
    }

    static func profileIndex(of id: String, in raw: JSONValue) -> Int? {
        raw["profiles"]?.arrayValue?.firstIndex { $0["id"]?.stringValue == id }
    }

    /// `a.b[2].c` → `[.key(a), .key(b), .index(2), .key(c)]`.
    static func parse(_ dotted: String) -> [PathComponent] {
        guard !dotted.isEmpty else { return [] }
        var out: [PathComponent] = []
        for part in dotted.split(separator: ".", omittingEmptySubsequences: false) {
            var rest = Substring(part)
            if let bracket = rest.firstIndex(of: "[") {
                let key = rest[..<bracket]
                if !key.isEmpty { out.append(.key(String(key))) }
                rest = rest[bracket...]
                while rest.hasPrefix("["), let close = rest.firstIndex(of: "]") {
                    let inner = rest[rest.index(after: rest.startIndex)..<close]
                    if let i = Int(inner) { out.append(.index(i)) } else { out.append(.key(String(inner))) }
                    rest = rest[rest.index(after: close)...]
                }
                if !rest.isEmpty { out.append(.key(String(rest))) }
            } else {
                out.append(.key(String(rest)))
            }
        }
        return out
    }

    /// Map a validator path (`profiles[1].limits.session.pause`, `display.menu_bar`, "") to a
    /// field path, resolving `profiles[i]` through the document that was validated.
    static func fromIssuePath(_ path: String, in raw: JSONValue) -> FieldPath {
        let prefix = "profiles["
        guard path.hasPrefix(prefix), let close = path.firstIndex(of: "]"),
              let index = Int(path[path.index(path.startIndex, offsetBy: prefix.count)..<close]),
              let id = raw["profiles"]?.arrayValue?[safe: index]?["id"]?.stringValue
        else {
            return .root(path)
        }
        var rest = path[path.index(after: close)...]
        if rest.hasPrefix(".") { rest = rest.dropFirst() }
        return .profile(id, String(rest))
    }
}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
