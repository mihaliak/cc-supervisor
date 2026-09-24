import Foundation

/// `ccs config validate --json` (P02): `{"ok", "issues": [{"path", "message"}], "revision", "path"}`.
/// Exit code 1 when invalid (the JSON is still printed).
struct ValidationReport: Decodable, Sendable, Equatable {
    struct Issue: Decodable, Sendable, Equatable {
        var path: String
        var message: String

        init(path: String, message: String) {
            self.path = path
            self.message = message
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            path = (try? c.decodeIfPresent(String.self, forKey: .path)) ?? ""
            message = (try? c.decodeIfPresent(String.self, forKey: .message)) ?? "invalid"
        }

        enum CodingKeys: String, CodingKey { case path, message }
    }

    var ok: Bool
    var issues: [Issue]
    var revision: Int?

    init(ok: Bool, issues: [Issue], revision: Int? = nil) {
        self.ok = ok
        self.issues = issues
        self.revision = revision
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        issues = (try? c.decodeIfPresent([Issue].self, forKey: .issues)) ?? []
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? issues.isEmpty
        revision = try? c.decodeIfPresent(Int.self, forKey: .revision)
    }

    enum CodingKeys: String, CodingKey { case ok, issues, revision }
}

/// Validation issues keyed by the field they belong to (ADR-0001: Swift only displays
/// what `ccs config validate` reports; it never validates itself).
struct ValidationErrors: Sendable, Equatable {
    struct Entry: Sendable, Equatable {
        var field: FieldPath
        var message: String
    }

    private(set) var entries: [Entry] = []

    init() {}

    /// Map the report's paths (`profiles[1].limits.session.pause`) through the document that
    /// was validated, so `profiles[i]` resolves to that profile's id.
    init(report: ValidationReport, validated raw: JSONValue) {
        entries = report.issues.map {
            Entry(field: FieldPath.fromIssuePath($0.path, in: raw), message: $0.message)
        }
    }

    var isEmpty: Bool { entries.isEmpty }

    /// Messages for exactly this field.
    func messages(for field: FieldPath) -> [String] {
        entries.filter { $0.field == field }.map(\.message)
    }

    /// Messages for this field and everything inside it (e.g. a whole section).
    func messages(within field: FieldPath) -> [String] {
        entries.filter { $0.field.isWithin(field) }.map(\.message)
    }

    /// Issues for a profile id (list badges).
    func hasIssues(profileID: String) -> Bool {
        entries.contains { $0.field.profileID == profileID }
    }

    /// Issues not attached to any profile (top-level keys or the document).
    var topLevel: [Entry] {
        entries.filter { $0.field.profileID == nil }
    }
}
