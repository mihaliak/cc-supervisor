import Foundation

enum MenuBarMode: String, Sendable, Equatable {
    case letterPercent = "letter_percent"
    case iconOnly = "icon_only"

    /// `emoji_percent` is the pre-ADR-0018 name of `letter_percent`, still accepted.
    static let legacyLetterPercent = "emoji_percent"

    init?(configValue: String) {
        self.init(rawValue: configValue == Self.legacyLetterPercent ? Self.letterPercent.rawValue : configValue)
    }

    /// The picker tag for a stored value (legacy names map to their current tag).
    static func normalized(_ configValue: String) -> String {
        MenuBarMode(configValue: configValue)?.rawValue ?? configValue
    }
}

/// The bits of `config.json` the app shell needs (ADR-0004). Read-only here;
/// P11 adds writes. A missing or unreadable file yields the defaults and is
/// never created by the app.
struct AppConfig: Sendable, Equatable {
    var ccsPath: String
    var menuBarMode: MenuBarMode
    var fileExists: Bool

    static func defaultCcsPath(home: URL = StateLocation.realHome()) -> String {
        home.appendingPathComponent(".local/bin/ccs").path
    }
}

enum ConfigReader {
    static func read(
        file: URL = StateLocation.configFile(),
        home: URL = StateLocation.realHome()
    ) -> AppConfig {
        guard let data = try? Data(contentsOf: file) else {
            return AppConfig(ccsPath: AppConfig.defaultCcsPath(home: home), menuBarMode: .letterPercent, fileExists: false)
        }
        return parse(data, home: home)
    }

    static func parse(_ data: Data, home: URL = StateLocation.realHome()) -> AppConfig {
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        var ccsPath = AppConfig.defaultCcsPath(home: home)
        if let raw = object["ccs_path"] as? String, !raw.isEmpty {
            ccsPath = expandTilde(raw, home: home)
        }
        let display = object["display"] as? [String: Any]
        let mode = (display?["menu_bar"] as? String).flatMap(MenuBarMode.init(configValue:)) ?? .letterPercent
        return AppConfig(ccsPath: ccsPath, menuBarMode: mode, fileExists: true)
    }

    static func expandTilde(_ path: String, home: URL) -> String {
        if path == "~" { return home.path }
        if path.hasPrefix("~/") { return home.appendingPathComponent(String(path.dropFirst(2))).path }
        return path
    }
}
