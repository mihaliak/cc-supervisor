@testable import CCSupervisor
import XCTest

/// ADR-0009 vectors shared with Python (`schema/fixtures/time_format.json`).
final class TimeFormatTests: XCTestCase {
    private struct Vector: Decodable {
        let name: String
        let tz: String
        let now: String
        let reset: String
        let absolute: String
        let relative: String
        let combined: String
        let compact: String
    }

    func testSharedVectors() throws {
        let vectors = try JSONDecoder().decode([Vector].self, from: try Fixtures.data("time_format.json"))
        XCTAssertGreaterThanOrEqual(vectors.count, 20)
        for v in vectors {
            let tz = try XCTUnwrap(TimeZone(identifier: v.tz), v.name)
            let now = Fixtures.date(v.now)
            let reset = Fixtures.date(v.reset)
            XCTAssertEqual(TimeFormat.absolute(reset, now: now, timeZone: tz), v.absolute, v.name)
            XCTAssertEqual(TimeFormat.relative(reset, now: now), v.relative, v.name)
            XCTAssertEqual(TimeFormat.combined(reset, now: now, timeZone: tz), v.combined, v.name)
            XCTAssertEqual(TimeFormat.compact(reset, now: now, timeZone: tz), v.compact, v.name)
        }
    }

    func testAgo() {
        let now = Fixtures.date("2026-09-24T15:47:00Z")
        XCTAssertEqual(TimeFormat.ago(now.addingTimeInterval(-30), now: now), "just now")
        XCTAssertEqual(TimeFormat.ago(now.addingTimeInterval(-14 * 60 - 20), now: now), "14m ago")
        XCTAssertEqual(TimeFormat.ago(now.addingTimeInterval(-(2 * 3600 + 5 * 60)), now: now), "2h 5m ago")
        XCTAssertEqual(TimeFormat.ago(now.addingTimeInterval(-(27 * 3600)), now: now), "1d 3h ago")
    }
}
