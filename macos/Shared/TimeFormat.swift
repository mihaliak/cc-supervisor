import Foundation

/// Reset-time formatting per ADR-0009 — presentation only, locale-independent.
/// Mirrors `ccs.timefmt`; both must pass `schema/fixtures/time_format.json`.
public enum TimeFormat {
    private static let weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  // Calendar weekday 1 = Sunday
    private static let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    /// `HH:MM` today, `Ddd HH:MM` within the next 6 local days, else `D Mon HH:MM`.
    /// The clock time is rounded to the nearest minute (`07:59:59.9` shows as `08:00`).
    public static func absolute(_ reset: Date, now: Date, timeZone: TimeZone = .current) -> String {
        let reset = reset.addingTimeInterval(30)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let r = calendar.dateComponents([.year, .month, .day, .hour, .minute, .weekday], from: reset)
        let hm = String(format: "%02d:%02d", r.hour ?? 0, r.minute ?? 0)
        let days = calendar.dateComponents(
            [.day],
            from: calendar.startOfDay(for: now),
            to: calendar.startOfDay(for: reset)
        ).day ?? 0
        if days == 0 { return hm }
        if days > 0 && days < 7 { return "\(weekdays[(r.weekday ?? 1) - 1]) \(hm)" }
        return "\(r.day ?? 1) \(months[(r.month ?? 1) - 1]) \(hm)"
    }

    /// `now`, `in <1m`, `in 42m`, `in 2h 13m`, `in 2h`, `in 3d 4h`, `in 3d` (rounded down).
    public static func relative(_ reset: Date, now: Date) -> String {
        let seconds = reset.timeIntervalSince(now)
        if seconds <= 0 { return "now" }
        if seconds < 60 { return "in <1m" }
        return "in " + duration(minutes: Int((seconds / 60).rounded(.down)))
    }

    /// Statusline form: `20:00 (in 2h 13m)`.
    public static func combined(_ reset: Date, now: Date, timeZone: TimeZone = .current) -> String {
        "\(absolute(reset, now: now, timeZone: timeZone)) (\(relative(reset, now: now)))"
    }

    /// Widget / menu bar form: `20:00 · in 2h 13m`.
    public static func compact(_ reset: Date, now: Date, timeZone: TimeZone = .current) -> String {
        "\(absolute(reset, now: now, timeZone: timeZone)) · \(relative(reset, now: now))"
    }

    /// Age of a past timestamp: `just now`, `14m ago`, `2h 5m ago`, `1d 3h ago`.
    public static func ago(_ date: Date, now: Date) -> String {
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "just now" }
        return duration(minutes: Int((seconds / 60).rounded(.down))) + " ago"
    }

    /// `42m`, `2h 13m`, `2h`, `3d 4h`, `3d` for a whole number of minutes (>= 1).
    static func duration(minutes total: Int) -> String {
        if total < 60 { return "\(total)m" }
        let hours = total / 60
        let mins = total % 60
        if hours < 24 { return mins == 0 ? "\(hours)h" : "\(hours)h \(mins)m" }
        let days = hours / 24
        let remHours = hours % 24
        return remHours == 0 ? "\(days)d" : "\(days)d \(remHours)h"
    }
}
