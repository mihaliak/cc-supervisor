import Foundation

/// `ccsupervisor://` links (ADR-0011): `profile/<id>`, `signin/<id>`, `refresh`.
public enum DeepLink: Equatable, Sendable {
    case profile(String)
    case signIn(String)
    case refresh

    public static let scheme = "ccsupervisor"

    public init?(url: URL) {
        guard url.scheme?.lowercased() == Self.scheme else { return nil }
        // `ccsupervisor://profile/work` parses with host "profile" and path "/work".
        var parts: [String] = []
        if let host = url.host(percentEncoded: false), !host.isEmpty { parts.append(host) }
        parts += url.pathComponents.filter { $0 != "/" && !$0.isEmpty }
        guard let head = parts.first?.lowercased() else { return nil }
        switch (head, parts.count) {
        case ("refresh", 1):
            self = .refresh
        case ("profile", 2) where Self.isValidID(parts[1]):
            self = .profile(parts[1])
        case ("signin", 2) where Self.isValidID(parts[1]):
            self = .signIn(parts[1])
        default:
            return nil
        }
    }

    /// The link, or nil when the id isn't a valid profile id. Ids come from files other
    /// programs can write (the snapshot, daemon events), so they're checked with the same
    /// rule as parsing and never trusted to form a URL.
    public var url: URL? {
        switch self {
        case .profile(let id): Self.isValidID(id) ? URL(string: "\(Self.scheme)://profile/\(id)") : nil
        case .signIn(let id): Self.isValidID(id) ? URL(string: "\(Self.scheme)://signin/\(id)") : nil
        case .refresh: URL(string: "\(Self.scheme)://refresh")
        }
    }

    /// Profile ids are slugs (ADR-0004): `[a-z0-9][a-z0-9-]{0,31}`, the whole string.
    static func isValidID(_ id: String) -> Bool {
        let bytes = Array(id.utf8)
        guard let first = bytes.first, bytes.count <= 32, isSlugByte(first), first != UInt8(ascii: "-") else {
            return false
        }
        return bytes.allSatisfy(isSlugByte)
    }

    private static func isSlugByte(_ byte: UInt8) -> Bool {
        (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte)
            || (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
            || byte == UInt8(ascii: "-")
    }
}
