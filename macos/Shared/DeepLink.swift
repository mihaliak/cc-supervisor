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

    public var url: URL {
        switch self {
        case .profile(let id): URL(string: "\(Self.scheme)://profile/\(id)")!
        case .signIn(let id): URL(string: "\(Self.scheme)://signin/\(id)")!
        case .refresh: URL(string: "\(Self.scheme)://refresh")!
        }
    }

    /// Profile ids are slugs (ADR-0004): `^[a-z0-9][a-z0-9-]{0,31}$`.
    static func isValidID(_ id: String) -> Bool {
        id.range(of: "^[a-z0-9][a-z0-9-]{0,31}$", options: .regularExpression) != nil
    }
}
