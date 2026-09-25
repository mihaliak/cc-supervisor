@testable import CCSupervisor
import XCTest

final class JSONValueTests: XCTestCase {
    func testCanonicalTextIsByteIdenticalToPython() throws {
        // Written by `ccs.fsio.dumps_json` (python/tests/test_settings_fixtures.py keeps it so).
        let data = try Fixtures.data("config/python_written.json")
        let value = try JSONValue.parse(data)
        XCTAssertEqual(value.canonicalText(), String(decoding: data, as: UTF8.self))
    }

    func testIntsAndFloatsStayApart() throws {
        let value = try JSONValue.parse(#"{"a": 92, "b": 92.0, "c": -0.0, "d": 1e16, "e": true}"#)
        XCTAssertEqual(value["a"], .int(92))
        XCTAssertEqual(value["b"], .double(92))
        XCTAssertEqual(value["e"], .bool(true))
        XCTAssertEqual(value["e"]?.intValue, nil)
        XCTAssertEqual(value.canonicalText(), "{\n  \"a\": 92,\n  \"b\": 92.0,\n  \"c\": -0.0,\n  \"d\": 1e+16,\n  \"e\": true\n}\n")
    }

    func testPythonFloatRepr() {
        let cases: [(Double, String)] = [
            (0, "0.0"), (1, "1.0"), (0.1, "0.1"), (1e-4, "0.0001"), (1e-5, "1e-05"),
            (1e15, "1000000000000000.0"), (1e16, "1e+16"), (123.456, "123.456"),
            (1.5e-7, "1.5e-07"), (-2.5, "-2.5"), (1.2345678901234568e+17, "1.2345678901234568e+17"),
            (5e-324, "5e-324"),
        ]
        for (value, expected) in cases {
            XCTAssertEqual(JSONValue.pythonFloatRepr(value), expected, "\(value)")
        }
    }

    func testStringEscapingMatchesPython() {
        var out = ""
        JSONValue.writeString("a\"b\\c/d\n\t\u{01}\u{2028}é💼", into: &out)
        XCTAssertEqual(out, "\"a\\\"b\\\\c/d\\n\\t\\u0001\u{2028}é💼\"")
    }

    func testParseErrors() {
        XCTAssertThrowsError(try JSONValue.parse("{\"a\": }"))
        XCTAssertThrowsError(try JSONValue.parse("{\"a\": 1} x"))
        XCTAssertThrowsError(try JSONValue.parse("[01]"))
        XCTAssertEqual(try JSONValue.parse("\"\\ud83d\\udcbc\""), .string("💼"))
    }

    func testSetValueCreatesAndRemoves() {
        var doc = JSONValue.object(["profiles": .array([.object(["id": .string("w")])])])
        XCTAssertTrue(doc.setValue(.int(5), at: FieldPath.parse("profiles[0].limits.session.pause")))
        XCTAssertEqual(doc.value(at: FieldPath.parse("profiles[0].limits.session.pause")), .int(5))
        XCTAssertTrue(doc.setValue(nil, at: FieldPath.parse("profiles[0].limits.session.pause")))
        XCTAssertEqual(doc.value(at: FieldPath.parse("profiles[0].limits.session")), .object([:]))
        XCTAssertFalse(doc.setValue(.int(1), at: FieldPath.parse("profiles[3].x")))
    }
}

final class FieldPathTests: XCTestCase {
    func testParse() {
        XCTAssertEqual(
            FieldPath.parse("warmup.triggers.schedule[2].time"),
            [.key("warmup"), .key("triggers"), .key("schedule"), .index(2), .key("time")]
        )
        XCTAssertEqual(FieldPath.parse(""), [])
    }

    func testResolveByIdAndIssuePaths() throws {
        let raw = try JSONValue.parse(try Fixtures.data("config/invalid.json"))
        XCTAssertEqual(FieldPath.profile("lab", "flag").resolved(in: raw), [.key("profiles"), .index(1), .key("flag")])
        XCTAssertNil(FieldPath.profile("nope", "flag").resolved(in: raw))
        XCTAssertEqual(FieldPath.fromIssuePath("profiles[1].limits.session.warn", in: raw), .profile("lab", "limits.session.warn"))
        XCTAssertEqual(FieldPath.fromIssuePath("profiles[0]", in: raw), .profile("work", ""))
        XCTAssertEqual(FieldPath.fromIssuePath("display.menu_bar", in: raw), .root("display.menu_bar"))
        XCTAssertEqual(FieldPath.fromIssuePath("profiles[9].x", in: raw), .root("profiles[9].x"))
        XCTAssertTrue(FieldPath.profile("lab", "limits.session.warn").isWithin(.profile("lab", "limits.session")))
        XCTAssertFalse(FieldPath.profile("lab", "limits.sessions").isWithin(.profile("lab", "limits.session")))
        XCTAssertTrue(FieldPath.profile("lab", "warmup.triggers.schedule[0].time").isWithin(.profile("lab", "warmup.triggers.schedule")))
    }
}

final class ValidationErrorsTests: XCTestCase {
    func testMapsRealValidatorOutputToFields() throws {
        let report = try CcsClient.decoder.decode(ValidationReport.self, from: try Fixtures.data("ccs/config_validate_invalid.json"))
        XCTAssertFalse(report.ok)
        XCTAssertEqual(report.revision, 1)
        let raw = try JSONValue.parse(try Fixtures.data("config/invalid.json"))
        let errors = ValidationErrors(report: report, validated: raw)
        XCTAssertEqual(errors.messages(for: .profile("lab", "flag")), ["'model' is reserved (ccs or claude option)"])
        XCTAssertEqual(errors.messages(for: .profile("lab", "limits.session.warn")), ["warn (95) must be lower than pause (90)"])
        XCTAssertEqual(errors.messages(within: .profile("lab", "limits.session")).count, 1)
        XCTAssertEqual(errors.messages(for: .profile("lab", "warmup.triggers.schedule[0].time")), ["must be a 24h time HH:MM"])
        XCTAssertEqual(errors.messages(within: .profile("lab", "warmup.triggers.schedule[0]")).count, 1)
        XCTAssertEqual(errors.topLevel.map(\.field), [.root("display.menu_bar")])
        XCTAssertTrue(errors.hasIssues(profileID: "lab"))
        XCTAssertFalse(errors.hasIssues(profileID: "work"))
    }
}

final class ConfigModelTests: XCTestCase {
    func testDecodesFixtureAndIsLenient() throws {
        let raw = try JSONValue.parse(try Fixtures.data("config/python_written.json"))
        let model = ConfigModel.from(raw)
        XCTAssertEqual(model.revision, 7)
        XCTAssertEqual(model.profiles?.map(\.id), ["work", "personal"])
        XCTAssertEqual(model.profile(id: "work")?.limits?.session?.pause, 90)
        XCTAssertEqual(model.profile(id: "work")?.warmup?.triggers?.schedule?.first?.weekdays, ["mon", "tue"])
        XCTAssertEqual(model.notifications?.limitResume, false)

        var broken = raw
        broken.setValue(.string("x"), at: FieldPath.parse("profiles[0].limits.session.pause"))
        broken.setValue(.int(3), at: FieldPath.parse("profiles[1].id"))
        let lenient = ConfigModel.from(broken)
        XCTAssertNil(lenient.profile(id: "work")?.limits?.session?.pause)
        XCTAssertEqual(lenient.profile(id: "work")?.limits?.session?.warn, 80)
        XCTAssertEqual(lenient.profiles?.map(\.id), ["work"], "a profile without a string id is skipped")
    }
}

@MainActor
final class ConfigStoreTests: XCTestCase {
    private var dir: URL!
    private var file: URL!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-config-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        file = dir.appendingPathComponent("config.json")
        try Fixtures.data("config/python_written.json").write(to: file)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func disk() throws -> JSONValue {
        try JSONValue.parse(try Data(contentsOf: file))
    }

    /// Another writer (the CLI) changes the file.
    private func externalWrite(_ edit: (inout JSONValue) -> Void) throws {
        var doc = try disk()
        edit(&doc)
        let rev = doc["revision"]?.intValue ?? 0
        doc.setValue(.int(Int64(rev + 1)), at: [.key("revision")])
        try doc.canonicalData().write(to: file)
    }

    private let pause = FieldPath.profile("work", "limits.session.pause")
    private let weeklyPause = FieldPath.profile("work", "limits.weekly.pause")

    func testEditPreservesUnknownKeysAndBumpsRevision() async throws {
        let original = try disk()
        let store = ConfigStore(fileURL: file)
        XCTAssertTrue(store.canEdit)
        XCTAssertEqual(store.int(pause), 90)
        store.set(pause, .int(92))
        let result = await store.saveNow()
        XCTAssertEqual(result, .saved(revision: 8))

        var expected = original
        expected.setValue(.int(92), at: FieldPath.parse("profiles[0].limits.session.pause"))
        expected.setValue(.int(8), at: [.key("revision")])
        let written = try Data(contentsOf: file)
        XCTAssertEqual(String(decoding: written, as: UTF8.self), expected.canonicalText(), "same bytes Python would write")
        let after = try disk()
        for path in ["x_top_unknown", "display.x_display_note", "profiles[0].x_profile_unknown",
                     "profiles[0].limits.weekly.x_note", "profiles[0].warmup.triggers.schedule[0].x_entry",
                     "profiles[1].x_numbers"] {
            XCTAssertEqual(after.value(at: FieldPath.parse(path)), original.value(at: FieldPath.parse(path)), path)
        }
        XCTAssertTrue(store.pending.isEmpty)
        XCTAssertEqual(store.loadedRevision, 8)
    }

    func testConcurrentChangeToOtherFieldIsMerged() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(92))
        try externalWrite { $0.setValue(.int(97), at: FieldPath.parse("profiles[0].limits.weekly.pause")) }
        let result = await store.saveNow()
        XCTAssertEqual(result, .saved(revision: 9))
        let after = try disk()
        XCTAssertEqual(after.value(at: FieldPath.parse("profiles[0].limits.session.pause")), .int(92))
        XCTAssertEqual(after.value(at: FieldPath.parse("profiles[0].limits.weekly.pause")), .int(97))
        XCTAssertEqual(store.int(weeklyPause), 97)
        XCTAssertNil(store.conflict)
    }

    func testProfilesReorderedOnDiskStillHitTheRightProfile() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(91))
        try externalWrite { doc in
            let profiles = doc["profiles"]!.arrayValue!
            doc.setValue(.array(profiles.reversed()), at: [.key("profiles")])
        }
        _ = await store.saveNow()
        let after = try disk()
        XCTAssertEqual(after.value(at: FieldPath.parse("profiles[1].id")), .string("work"))
        XCTAssertEqual(after.value(at: FieldPath.parse("profiles[1].limits.session.pause")), .int(91))
    }

    func testSameFieldConflictKeepMine() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(92))
        try externalWrite { $0.setValue(.int(85), at: FieldPath.parse("profiles[0].limits.session.pause")) }
        let result = await store.saveNow()
        XCTAssertEqual(result, .conflict([pause]))
        XCTAssertEqual(store.conflict?.fields, [pause])
        XCTAssertFalse(store.canEdit, "editing is blocked while the conflict alert is up")
        XCTAssertEqual(try disk().value(at: FieldPath.parse("profiles[0].limits.session.pause")), .int(85), "nothing written")

        let resolved = await store.resolveConflict(.keepMine)
        XCTAssertEqual(resolved, .saved(revision: 9))
        XCTAssertEqual(try disk().value(at: FieldPath.parse("profiles[0].limits.session.pause")), .int(92))
        XCTAssertNil(store.conflict)
    }

    func testSameFieldConflictTakeTheirs() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(92))
        try externalWrite { $0.setValue(.int(85), at: FieldPath.parse("profiles[0].limits.session.pause")) }
        _ = await store.saveNow()
        let before = try Data(contentsOf: file)
        let resolved = await store.resolveConflict(.takeTheirs)
        XCTAssertEqual(resolved, .nothingToSave)
        XCTAssertEqual(try Data(contentsOf: file), before, "their version is adopted as is")
        XCTAssertEqual(store.int(pause), 85)
        XCTAssertTrue(store.pending.isEmpty)
        XCTAssertNil(store.conflict)
        XCTAssertTrue(store.canEdit)
    }

    func testSameValueOnDiskIsNotAConflict() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(92))
        try externalWrite { $0.setValue(.int(92), at: FieldPath.parse("profiles[0].limits.session.pause")) }
        let result = await store.saveNow()
        XCTAssertEqual(result, .nothingToSave)
        XCTAssertEqual(store.int(pause), 92)
        XCTAssertNil(store.conflict)
    }

    func testAtomicWriteKeepsModeAndLeavesNoTempFiles() async throws {
        try FileManager.default.setAttributes([.posixPermissions: 0o640], ofItemAtPath: file.path)
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(93))
        _ = await store.saveNow()
        let mode = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o640)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path), ["config.json"])
    }

    func testUndoingAnEditDropsIt() async throws {
        let store = ConfigStore(fileURL: file)
        store.set(pause, .int(92))
        store.set(pause, .int(90))
        XCTAssertTrue(store.pending.isEmpty)
        let result = await store.saveNow()
        XCTAssertEqual(result, .nothingToSave)
        XCTAssertEqual(try Data(contentsOf: file), try Fixtures.data("config/python_written.json"))
    }

    func testDebouncedSave() async throws {
        let store = ConfigStore(fileURL: file)
        store.saveDelay = .milliseconds(50)
        store.set(pause, .int(94))
        store.set(pause, .int(95))
        for _ in 0..<40 where !store.pending.isEmpty {
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertEqual(try disk().value(at: FieldPath.parse("profiles[0].limits.session.pause")), .int(95))
        XCTAssertEqual(try disk()["revision"], .int(8), "two quick edits, one write")
    }

    func testValidationAfterSaveMapsIssues() async throws {
        let store = ConfigStore(fileURL: file)
        store.validator = {
            .report(ValidationReport(ok: false, issues: [.init(path: "profiles[0].limits.session.warn", message: "warn (95) must be lower than pause (90)")]))
        }
        store.set(FieldPath.profile("work", "limits.session.warn"), .int(95))
        _ = await store.saveNow()
        XCTAssertTrue(store.isInvalid)
        XCTAssertEqual(store.issues(.profile("work", "limits.session.warn")), ["warn (95) must be lower than pause (90)"])
        XCTAssertEqual(try disk().value(at: FieldPath.parse("profiles[0].limits.session.warn")), .int(95), "invalid values stay written")

        store.validator = { .report(ValidationReport(ok: true, issues: [])) }
        await store.validate()
        XCTAssertFalse(store.isInvalid)
        XCTAssertTrue(store.errors.isEmpty)
    }

    /// Regression: the debounced autosave used to cancel its own task before validating,
    /// so a cancellable `ccs config validate` run ended in "Couldn't validate the config …
    /// CancellationError" (seen when switching the menu bar label).
    func testAutosaveValidationIsNotCancelledByItself() async throws {
        let store = ConfigStore(fileURL: file)
        store.saveDelay = .milliseconds(10)
        let validated = expectation(description: "validated")
        let probe = CancellationProbe()
        store.validator = {
            // Like the real `ccs` call: a wait that throws when its task is cancelled.
            do {
                try await Task.sleep(for: .milliseconds(100))
            } catch {
                probe.cancelled = true
                return .cancelled
            }
            validated.fulfill()
            return .report(ValidationReport(ok: true, issues: []))
        }
        store.set(.root("display.menu_bar"), .string("icon_only"))
        await fulfillment(of: [validated], timeout: 5)
        XCTAssertFalse(probe.cancelled)
        XCTAssertNil(store.validationProblem)
        XCTAssertFalse(store.isInvalid)
    }

    /// A newer edit during a running validation must not cancel or poison it, and a
    /// cancelled run never shows up as a validation problem.
    func testEditDuringValidationAndCancelledOutcome() async throws {
        let store = ConfigStore(fileURL: file)
        store.saveDelay = .milliseconds(10)
        let firstStarted = expectation(description: "first validation started")
        let bothDone = expectation(description: "two validations finished")
        bothDone.expectedFulfillmentCount = 2
        let probe = CancellationProbe()
        store.validator = {
            probe.runs += 1
            if probe.runs == 1 { firstStarted.fulfill() }
            do {
                try await Task.sleep(for: .milliseconds(150))
            } catch {
                probe.cancelled = true
                return .cancelled
            }
            bothDone.fulfill()
            return .report(ValidationReport(ok: true, issues: []))
        }
        store.set(.root("display.menu_bar"), .string("icon_only"))
        await fulfillment(of: [firstStarted], timeout: 5)
        store.set(.root("display.menu_bar"), .string("letter_percent"))
        await fulfillment(of: [bothDone], timeout: 5)
        XCTAssertFalse(probe.cancelled)
        XCTAssertNil(store.validationProblem)
        XCTAssertEqual(try disk().value(at: FieldPath.parse("display.menu_bar")), .string("letter_percent"))

        store.validator = { .cancelled }
        await store.validate()
        XCTAssertNil(store.validationProblem, "a cancelled run is not an error")
    }

    func testDefaultsFillMissingKeys() throws {
        try #"{"version": 1, "revision": 0, "profiles": [{"id": "w", "flag": "w", "name": "W", "emoji": "x", "config_dir": "~/w"}]}"#
            .write(to: file, atomically: true, encoding: .utf8)
        let store = ConfigStore(fileURL: file)
        XCTAssertNil(store.int(.profile("w", "limits.session.pause")))
        store.setDefaults(.object([
            "config": .object(["display": .object(["menu_bar": .string("emoji_percent")])]),
            "profile": .object(["limits": .object(["session": .object(["pause": .int(90)])])]),
        ]))
        XCTAssertEqual(store.int(.profile("w", "limits.session.pause")), 90)
        XCTAssertEqual(store.string(.root("display.menu_bar")), "emoji_percent")
        XCTAssertNil(store.string(.root("ccs_path")))
    }

    func testMissingFileIsNeverCreated() async throws {
        try FileManager.default.removeItem(at: file)
        let store = ConfigStore(fileURL: file)
        XCTAssertFalse(store.fileExists)
        XCTAssertFalse(store.canEdit)
        store.set(.root("display.menu_bar"), .string("icon_only"))
        let result = await store.saveNow()
        XCTAssertEqual(result, .nothingToSave)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))

        try Fixtures.data("config/python_written.json").write(to: file)
        XCTAssertTrue(store.reloadIfChanged())
        XCTAssertTrue(store.canEdit)
        XCTAssertEqual(store.profiles.count, 2)
    }

    func testBrokenFileIsNotOverwritten() async throws {
        try "{ not json".write(to: file, atomically: true, encoding: .utf8)
        let store = ConfigStore(fileURL: file)
        XCTAssertNotNil(store.loadError)
        XCTAssertFalse(store.canEdit)
        store.set(pause, .int(92))
        _ = await store.saveNow()
        XCTAssertEqual(try String(contentsOf: file, encoding: .utf8), "{ not json")
    }

    func testReloadIfChangedSkipsWhileEditing() throws {
        let store = ConfigStore(fileURL: file)
        XCTAssertFalse(store.reloadIfChanged(), "unchanged")
        store.set(pause, .int(92))
        try externalWrite { $0.setValue(.int(97), at: FieldPath.parse("profiles[0].limits.weekly.pause")) }
        XCTAssertFalse(store.reloadIfChanged(), "pending edits are never discarded")
        XCTAssertEqual(store.int(pause), 92)
    }
}

final class SettingsComponentsTests: XCTestCase {
    func testTimeOfDayRoundTripsEveryMinuteAcrossDSTZones() {
        for zone in ["America/New_York", "Europe/Bratislava", "Australia/Lord_Howe", "Asia/Kolkata", "UTC"] {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(identifier: zone)!
            for minute in 0..<(24 * 60) {
                let text = String(format: "%02d:%02d", minute / 60, minute % 60)
                guard let date = TimeOfDay.date(from: text, calendar: calendar) else {
                    return XCTFail("\(zone) \(text) did not convert")
                }
                XCTAssertEqual(TimeOfDay.string(from: date, calendar: calendar), text, "\(zone) \(text)")
            }
        }
    }

    func testTimeOfDayRejectsBadStrings() {
        for bad in ["6:00", "24:00", "12:60", "12-00", "", "1200"] {
            XCTAssertNil(TimeOfDay.date(from: bad), bad)
        }
    }

    func testWeekdaysToggleKeepsWeekOrder() {
        XCTAssertEqual(Weekdays.toggled(["fri", "mon"], "wed"), ["mon", "wed", "fri"])
        XCTAssertEqual(Weekdays.toggled(["mon", "wed"], "mon"), ["wed"])
        XCTAssertEqual(Weekdays.toggled([], "sun"), ["sun"])
        XCTAssertEqual(Weekdays.label("thu"), "Thu")
    }

    func testDirectoryAbbreviation() {
        let home = URL(fileURLWithPath: "/Users/me")
        XCTAssertEqual(DirectoryField.abbreviate("/Users/me/.claude-work", home: home), "~/.claude-work")
        XCTAssertEqual(DirectoryField.abbreviate("/Users/me", home: home), "~")
        XCTAssertEqual(DirectoryField.abbreviate("/Users/meow/x", home: home), "/Users/meow/x")
    }
}

/// Mutable flags shared with a validator closure (all on the main actor).
@MainActor
private final class CancellationProbe {
    var cancelled = false
    var runs = 0
}
