import Foundation

/// Display model written by the daemon to `widget/snapshot.json` (ADR-0005,
/// `schema/widget-snapshot.schema.json`). Everything is precomputed in Python
/// (ADR-0001); Swift only decodes and renders. Decoding is lenient: unknown
/// enum values and missing non-essential fields fall back to safe defaults.
public struct WidgetSnapshot: Codable, Sendable, Equatable {
    public var schema: Int
    public var generatedAt: Date?
    public var profiles: [ProfileSnapshot]

    /// A snapshot older than this means the daemon is offline (ADR-0005/0009).
    public static let offlineAfter: TimeInterval = 5 * 60

    public init(schema: Int = 1, generatedAt: Date?, profiles: [ProfileSnapshot]) {
        self.schema = schema
        self.generatedAt = generatedAt
        self.profiles = profiles
    }

    enum CodingKeys: String, CodingKey {
        case schema
        case generatedAt = "generated_at"
        case profiles
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = (try? c.decodeIfPresent(Int.self, forKey: .schema)) ?? 1
        generatedAt = c.lenientDate(.generatedAt)
        profiles = (try? c.decodeIfPresent([ProfileSnapshot].self, forKey: .profiles)) ?? []
    }

    /// True when the snapshot is missing a timestamp or is older than `offlineAfter`.
    public func isDaemonOffline(now: Date) -> Bool {
        guard let generatedAt else { return true }
        return now.timeIntervalSince(generatedAt) > Self.offlineAfter
    }

    public func profile(id: String) -> ProfileSnapshot? {
        profiles.first { $0.id == id }
    }

    /// Worst level across profiles (red > yellow > green); nil without any level.
    public var worstLevel: Level? {
        profiles.compactMap(\.level).max()
    }
}

public enum Level: String, Codable, Sendable, Comparable, CaseIterable {
    case green, yellow, red

    private var rank: Int {
        switch self {
        case .green: 0
        case .yellow: 1
        case .red: 2
        }
    }

    public static func < (lhs: Level, rhs: Level) -> Bool { lhs.rank < rhs.rank }
}

public enum ProfileStatus: String, Codable, Sendable {
    case ok
    case needsSignIn = "needs_sign_in"
    case noSubscription = "no_subscription"
    case sourceError = "source_error"
    case stale
    case noData = "no_data"
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = ProfileStatus(rawValue: raw) ?? .unknown
    }
}

public struct ProfileSnapshot: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var emoji: String
    public var status: ProfileStatus
    public var stale: Bool
    public var level: Level?
    public var updatedAt: Date?
    public var rows: [UsageRow]
    public var supervisor: SupervisorSummary

    public init(
        id: String,
        name: String,
        emoji: String,
        status: ProfileStatus,
        stale: Bool = false,
        level: Level? = nil,
        updatedAt: Date? = nil,
        rows: [UsageRow] = [],
        supervisor: SupervisorSummary = SupervisorSummary()
    ) {
        self.id = id
        self.name = name
        self.emoji = emoji
        self.status = status
        self.stale = stale
        self.level = level
        self.updatedAt = updatedAt
        self.rows = rows
        self.supervisor = supervisor
    }

    enum CodingKeys: String, CodingKey {
        case id, name, emoji, status, stale, level, rows, supervisor
        case updatedAt = "updated_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = (try? c.decodeIfPresent(String.self, forKey: .name)) ?? id
        emoji = (try? c.decodeIfPresent(String.self, forKey: .emoji)) ?? ""
        status = (try? c.decodeIfPresent(ProfileStatus.self, forKey: .status)) ?? .unknown
        stale = (try? c.decodeIfPresent(Bool.self, forKey: .stale)) ?? false
        level = c.lenientLevel(.level)
        updatedAt = c.lenientDate(.updatedAt)
        rows = (try? c.decodeIfPresent([UsageRow].self, forKey: .rows)) ?? []
        supervisor = (try? c.decodeIfPresent(SupervisorSummary.self, forKey: .supervisor)) ?? SupervisorSummary()
    }

    /// The 5-hour session row, if present.
    public var sessionRow: UsageRow? { rows.first { $0.kind == .session } }
}

public enum RowKind: String, Codable, Sendable {
    case session
    case weekly
    case modelScoped = "model_scoped"
    case extraUsage = "extra_usage"
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = RowKind(rawValue: raw) ?? .unknown
    }
}

public struct UsageRow: Codable, Sendable, Equatable, Identifiable {
    public var kind: RowKind
    public var label: String
    public var percent: Int
    public var level: Level?
    public var resetsAt: Date?
    public var detail: String?

    public var id: String { "\(kind.rawValue):\(label)" }

    public init(kind: RowKind, label: String, percent: Int, level: Level?, resetsAt: Date? = nil, detail: String? = nil) {
        self.kind = kind
        self.label = label
        self.percent = percent
        self.level = level
        self.resetsAt = resetsAt
        self.detail = detail
    }

    enum CodingKeys: String, CodingKey {
        case kind, label, percent, level, detail
        case resetsAt = "resets_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = (try? c.decodeIfPresent(RowKind.self, forKey: .kind)) ?? .unknown
        label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? ""
        percent = (try? c.decodeIfPresent(Int.self, forKey: .percent)) ?? 0
        level = c.lenientLevel(.level)
        resetsAt = c.lenientDate(.resetsAt)
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
    }
}

public enum SupervisorState: String, Codable, Sendable {
    case normal, warned, paused, unknown

    public init(from decoder: Decoder) throws {
        let raw = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
        self = SupervisorState(rawValue: raw) ?? .unknown
    }
}

public struct SupervisorSummary: Codable, Sendable, Equatable {
    public var state: SupervisorState
    public var activeSessions: Int
    public var pausedSessions: Int
    public var otherSessions: Int
    public var resumeAt: Date?
    public var nextWarmupAt: Date?

    public init(
        state: SupervisorState = .normal,
        activeSessions: Int = 0,
        pausedSessions: Int = 0,
        otherSessions: Int = 0,
        resumeAt: Date? = nil,
        nextWarmupAt: Date? = nil
    ) {
        self.state = state
        self.activeSessions = activeSessions
        self.pausedSessions = pausedSessions
        self.otherSessions = otherSessions
        self.resumeAt = resumeAt
        self.nextWarmupAt = nextWarmupAt
    }

    enum CodingKeys: String, CodingKey {
        case state
        case activeSessions = "active_sessions"
        case pausedSessions = "paused_sessions"
        case otherSessions = "other_sessions"
        case resumeAt = "resume_at"
        case nextWarmupAt = "next_warmup_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = (try? c.decodeIfPresent(SupervisorState.self, forKey: .state)) ?? .normal
        activeSessions = (try? c.decodeIfPresent(Int.self, forKey: .activeSessions)) ?? 0
        pausedSessions = (try? c.decodeIfPresent(Int.self, forKey: .pausedSessions)) ?? 0
        otherSessions = (try? c.decodeIfPresent(Int.self, forKey: .otherSessions)) ?? 0
        resumeAt = c.lenientDate(.resumeAt)
        nextWarmupAt = c.lenientDate(.nextWarmupAt)
    }
}

// MARK: - Decoding helpers

public enum SnapshotDecoding {
    /// Decode `widget/snapshot.json` bytes.
    public static func decode(_ data: Data) throws -> WidgetSnapshot {
        try JSONDecoder().decode(WidgetSnapshot.self, from: data)
    }

    /// Parse the ISO 8601 timestamps Python writes (`…Z`, optional fraction or offset).
    public static func parseDate(_ string: String) -> Date? {
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: string) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: string)
    }
}

extension KeyedDecodingContainer {
    func lenientDate(_ key: Key) -> Date? {
        guard let raw = try? decodeIfPresent(String.self, forKey: key) else { return nil }
        return SnapshotDecoding.parseDate(raw)
    }

    func lenientLevel(_ key: Key) -> Level? {
        guard let raw = try? decodeIfPresent(String.self, forKey: key) else { return nil }
        return Level(rawValue: raw)
    }
}
