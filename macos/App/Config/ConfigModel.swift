import Foundation

/// Typed, read-only mirror of `config.json` v1 (ADR-0004) for display. Every field is
/// optional and decoded leniently: a wrong type yields `nil` instead of failing the whole
/// document. Writes never go through this model (see `ConfigStore`, which edits the raw tree).
struct ConfigModel: Codable, Sendable, Equatable {
    var version: Int?
    var revision: Int?
    var defaultProfile: String?
    var ccsPath: String?
    var claudePath: String?
    var display: Display?
    var polling: Polling?
    var notifications: Notifications?
    var profiles: [Profile]?

    enum CodingKeys: String, CodingKey {
        case version, revision, display, polling, notifications, profiles
        case defaultProfile = "default_profile"
        case ccsPath = "ccs_path"
        case claudePath = "claude_path"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = c.lossy(Int.self, .version)
        revision = c.lossy(Int.self, .revision)
        defaultProfile = c.lossy(String.self, .defaultProfile)
        ccsPath = c.lossy(String.self, .ccsPath)
        claudePath = c.lossy(String.self, .claudePath)
        display = c.lossy(Display.self, .display)
        polling = c.lossy(Polling.self, .polling)
        notifications = c.lossy(Notifications.self, .notifications)
        profiles = c.lossyArray(Profile.self, .profiles)
    }

    /// Decode from the raw tree; a non-object document gives an empty model.
    static func from(_ raw: JSONValue) -> ConfigModel {
        (try? JSONDecoder().decode(ConfigModel.self, from: raw.canonicalData())) ?? ConfigModel()
    }

    func profile(id: String) -> Profile? {
        profiles?.first { $0.id == id }
    }

    struct Display: Codable, Sendable, Equatable {
        var timeFormat: String?
        var menuBar: String?
        var colors: Colors?

        enum CodingKeys: String, CodingKey {
            case colors
            case timeFormat = "time_format"
            case menuBar = "menu_bar"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            timeFormat = c.lossy(String.self, .timeFormat)
            menuBar = c.lossy(String.self, .menuBar)
            colors = c.lossy(Colors.self, .colors)
        }
    }

    struct Colors: Codable, Sendable, Equatable {
        var yellowFrom: Int?
        var redFrom: Int?

        enum CodingKeys: String, CodingKey {
            case yellowFrom = "yellow_from"
            case redFrom = "red_from"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            yellowFrom = c.lossy(Int.self, .yellowFrom)
            redFrom = c.lossy(Int.self, .redFrom)
        }
    }

    struct Polling: Codable, Sendable, Equatable {
        var intervalSeconds: Int?
        var fastIntervalSeconds: Int?
        var fastWhenPercentAtLeast: Int?
        var idleIntervalSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case intervalSeconds = "interval_seconds"
            case fastIntervalSeconds = "fast_interval_seconds"
            case fastWhenPercentAtLeast = "fast_when_percent_at_least"
            case idleIntervalSeconds = "idle_interval_seconds"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            intervalSeconds = c.lossy(Int.self, .intervalSeconds)
            fastIntervalSeconds = c.lossy(Int.self, .fastIntervalSeconds)
            fastWhenPercentAtLeast = c.lossy(Int.self, .fastWhenPercentAtLeast)
            idleIntervalSeconds = c.lossy(Int.self, .idleIntervalSeconds)
        }
    }

    struct Notifications: Codable, Sendable, Equatable {
        var limitWarn: Bool?
        var limitPause: Bool?
        var limitResume: Bool?
        var warmup: Bool?
        var errors: Bool?

        enum CodingKeys: String, CodingKey {
            case warmup, errors
            case limitWarn = "limit_warn"
            case limitPause = "limit_pause"
            case limitResume = "limit_resume"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            limitWarn = c.lossy(Bool.self, .limitWarn)
            limitPause = c.lossy(Bool.self, .limitPause)
            limitResume = c.lossy(Bool.self, .limitResume)
            warmup = c.lossy(Bool.self, .warmup)
            errors = c.lossy(Bool.self, .errors)
        }
    }

    struct Profile: Codable, Sendable, Equatable, Identifiable {
        var id: String
        var flag: String?
        var name: String?
        var emoji: String?
        var configDir: String?
        var limits: Limits?
        var supervisor: Supervisor?
        var statusline: Statusline?
        var warmup: Warmup?

        enum CodingKeys: String, CodingKey {
            case id, flag, name, emoji, limits, supervisor, statusline, warmup
            case configDir = "config_dir"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(String.self, forKey: .id)
            flag = c.lossy(String.self, .flag)
            name = c.lossy(String.self, .name)
            emoji = c.lossy(String.self, .emoji)
            configDir = c.lossy(String.self, .configDir)
            limits = c.lossy(Limits.self, .limits)
            supervisor = c.lossy(Supervisor.self, .supervisor)
            statusline = c.lossy(Statusline.self, .statusline)
            warmup = c.lossy(Warmup.self, .warmup)
        }

        var displayName: String { name?.isEmpty == false ? name! : id }
    }

    struct WarnPause: Codable, Sendable, Equatable {
        var warn: Int?
        var pause: Int?
        var warnOnly: Bool?
        var spill: Bool?

        enum CodingKeys: String, CodingKey {
            case warn, pause, spill
            case warnOnly = "warn_only"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            warn = c.lossy(Int.self, .warn)
            pause = c.lossy(Int.self, .pause)
            warnOnly = c.lossy(Bool.self, .warnOnly)
            spill = c.lossy(Bool.self, .spill)
        }
    }

    struct Limits: Codable, Sendable, Equatable {
        var session: WarnPause?
        var weekly: WarnPause?
        var modelScoped: WarnPause?
        var extraUsage: WarnPause?

        enum CodingKeys: String, CodingKey {
            case session, weekly
            case modelScoped = "model_scoped"
            case extraUsage = "extra_usage"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            session = c.lossy(WarnPause.self, .session)
            weekly = c.lossy(WarnPause.self, .weekly)
            modelScoped = c.lossy(WarnPause.self, .modelScoped)
            extraUsage = c.lossy(WarnPause.self, .extraUsage)
        }
    }

    struct Supervisor: Codable, Sendable, Equatable {
        var enabled: Bool?
        var resumePrompt: String?

        enum CodingKeys: String, CodingKey {
            case enabled
            case resumePrompt = "resume_prompt"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            enabled = c.lossy(Bool.self, .enabled)
            resumePrompt = c.lossy(String.self, .resumePrompt)
        }
    }

    struct Statusline: Codable, Sendable, Equatable {
        var enabled: Bool?

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            enabled = c.lossy(Bool.self, .enabled)
        }
    }

    struct ScheduleEntry: Codable, Sendable, Equatable {
        var time: String?
        var weekdays: [String]?

        init(time: String?, weekdays: [String]?) {
            self.time = time
            self.weekdays = weekdays
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            time = c.lossy(String.self, .time)
            weekdays = c.lossy([String].self, .weekdays)
        }
    }

    struct Triggers: Codable, Sendable, Equatable {
        var schedule: [ScheduleEntry]?
        var appStart: Bool?
        var unlockWake: Bool?
        var autoChain: Bool?

        enum CodingKeys: String, CodingKey {
            case schedule
            case appStart = "app_start"
            case unlockWake = "unlock_wake"
            case autoChain = "auto_chain"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            schedule = c.lossyArray(ScheduleEntry.self, .schedule)
            appStart = c.lossy(Bool.self, .appStart)
            unlockWake = c.lossy(Bool.self, .unlockWake)
            autoChain = c.lossy(Bool.self, .autoChain)
        }
    }

    struct ActiveHours: Codable, Sendable, Equatable {
        var start: String?
        var end: String?

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            start = c.lossy(String.self, .start)
            end = c.lossy(String.self, .end)
        }
    }

    struct Warmup: Codable, Sendable, Equatable {
        var enabled: Bool?
        var model: String?
        var prompt: String?
        var triggers: Triggers?
        var activeHours: ActiveHours?
        var cooldownMinutes: Int?

        enum CodingKeys: String, CodingKey {
            case enabled, model, prompt, triggers
            case activeHours = "active_hours"
            case cooldownMinutes = "cooldown_minutes"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            enabled = c.lossy(Bool.self, .enabled)
            model = c.lossy(String.self, .model)
            prompt = c.lossy(String.self, .prompt)
            triggers = c.lossy(Triggers.self, .triggers)
            activeHours = c.lossy(ActiveHours.self, .activeHours)
            cooldownMinutes = c.lossy(Int.self, .cooldownMinutes)
        }
    }
}

extension KeyedDecodingContainer {
    /// Decode or `nil` (missing key, `null`, or a wrong type).
    func lossy<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }

    /// Decode an array, dropping elements that fail (e.g. a profile without an id).
    func lossyArray<T: Decodable>(_ type: T.Type, _ key: Key) -> [T]? {
        guard let items = try? decodeIfPresent([LossyElement<T>].self, forKey: key) else { return nil }
        return items.compactMap(\.value)
    }
}

private struct LossyElement<T: Decodable>: Decodable {
    let value: T?

    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}
