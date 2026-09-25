@testable import CCSupervisor
import XCTest

/// P12: widget display model, timeline entries, snapshot loading, profile catalog.
final class WidgetDisplayTests: XCTestCase {
    private let tz = TimeZone(identifier: "Europe/Bratislava")!

    private func snapshot(_ name: String) throws -> WidgetSnapshot {
        try SnapshotDecoding.decode(try Fixtures.data("snapshot/\(name).json"))
    }

    private func build(
        _ snap: WidgetSnapshot?,
        _ id: String?,
        at iso: String,
        options: WidgetOptions = WidgetOptions()
    ) -> WidgetDisplay {
        WidgetDisplayBuilder.build(snapshot: snap, profileID: id, options: options, now: Fixtures.date(iso), timeZone: tz)
    }

    // MARK: - States

    func testNotConfigured() throws {
        let noProfile = build(try snapshot("ok"), nil, at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(noProfile.state, .notConfigured)
        XCTAssertEqual(noProfile.message, "Choose a profile")
        XCTAssertNil(noProfile.url)
        XCTAssertFalse(noProfile.showsValues)

        let unknownID = build(try snapshot("ok"), "ghost", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(unknownID.state, .notConfigured)
    }

    func testOkWorkProfile() throws {
        let d = build(try snapshot("ok"), "work", at: "2026-09-24T15:47:00Z")
        XCTAssertEqual(d.state, .ok)
        XCTAssertEqual(d.title, "💼 Work")
        XCTAssertFalse(d.dimmed)
        XCTAssertNil(d.footer)
        XCTAssertTrue(d.showsValues)
        XCTAssertEqual(d.session?.percentText, "45%")
        XCTAssertEqual(d.session?.level, .green)
        XCTAssertEqual(d.session?.trailing, "20:00 · in 2h 13m")
        XCTAssertEqual(d.weeklyLine?.text, "W 72% · Sat 08:00")
        XCTAssertEqual(d.weeklyLine?.level, .yellow)
        XCTAssertEqual(d.rows.map(\.kind), [.session, .weekly, .modelScoped])
        XCTAssertEqual(d.rows[2].label, "Fable")
        XCTAssertEqual(d.supervisorLine, "2 ccs sessions · 3 other")
        XCTAssertEqual(d.nextWarmupText, "Next warm-up Fri 06:00 · in 12h 13m")
        XCTAssertEqual(d.updatedText, "Updated just now")
        XCTAssertFalse(d.isPaused)
        XCTAssertNil(d.resumeText)
        XCTAssertEqual(d.url, URL(string: "ccsupervisor://profile/work"))
    }

    func testOfflineWhenSnapshotOldAtLoad() throws {
        let d = build(try snapshot("offline"), "work", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.state, .offline)
        XCTAssertEqual(d.footer, "Supervisor offline")
        XCTAssertTrue(d.dimmed)
        XCTAssertTrue(d.showsValues, "last values stay visible, dimmed")
        XCTAssertEqual(d.url, URL(string: "ccsupervisor://profile/work"))
    }

    func testOfflineWhenSnapshotMissing() {
        let d = build(nil, "work", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.state, .offline)
        XCTAssertEqual(d.footer, "Supervisor offline")
        XCTAssertEqual(d.message, "No usage data yet")
        XCTAssertFalse(d.showsValues)
        XCTAssertEqual(d.url, URL(string: "ccsupervisor://profile/work"))
    }

    func testOfflineIsJudgedAtLoadNotAtLaterEntries() throws {
        let snap = try snapshot("ok")  // generated 15:47:10
        let later = Fixtures.date("2026-09-24T15:55:00Z")
        let loadedFresh = WidgetDisplayBuilder.build(
            snapshot: snap, profileID: "work", now: later,
            loadedAt: Fixtures.date("2026-09-24T15:47:30Z"), timeZone: tz
        )
        XCTAssertEqual(loadedFresh.state, .ok, "a future entry can't know the daemon died")
        let loadedLate = WidgetDisplayBuilder.build(snapshot: snap, profileID: "work", now: later, timeZone: tz)
        XCTAssertEqual(loadedLate.state, .offline)
    }

    func testNeedsSignIn() throws {
        let d = build(try snapshot("needs_sign_in"), "work", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.state, .needsSignIn)
        XCTAssertEqual(d.message, "Sign in required")
        XCTAssertFalse(d.showsValues)
        // ADR-0003/0012: the tap opens the profile's settings (which has Sign in).
        XCTAssertEqual(d.url, URL(string: "ccsupervisor://profile/work"))
    }

    func testStaleFromSnapshotFlag() throws {
        let d = build(try snapshot("stale"), "work", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.state, .stale)
        XCTAssertEqual(d.footer, "updated 14m ago")
        XCTAssertTrue(d.dimmed)
        XCTAssertTrue(d.showsValues)
    }

    func testStaleFromUpdatedAtAge() throws {
        var snap = try snapshot("ok")
        let now = Fixtures.date("2026-09-24T15:58:30Z")
        snap.generatedAt = now.addingTimeInterval(-20)  // daemon alive
        let d = WidgetDisplayBuilder.build(snapshot: snap, profileID: "work", now: now, timeZone: tz)
        XCTAssertEqual(d.state, .stale, "updated_at 15:47:00 is 11m old")
        XCTAssertEqual(d.footer, "updated 11m ago")

        let fresh = WidgetDisplayBuilder.build(
            snapshot: snap, profileID: "work", now: Fixtures.date("2026-09-24T15:56:00Z"), timeZone: tz
        )
        XCTAssertEqual(fresh.state, .ok, "9m old is not stale yet")
    }

    func testNoData() throws {
        let d = build(try snapshot("no_data"), "lab", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.state, .noData)
        XCTAssertEqual(d.message, "Usage unavailable")
        XCTAssertFalse(d.showsValues)
        XCTAssertNil(d.session)

        var snap = try snapshot("no_data")
        snap.profiles[0].status = .noSubscription
        XCTAssertEqual(build(snap, "lab", at: "2026-09-24T15:47:10Z").message, "No plan limits for this account")
        snap.profiles[0].status = .noData
        XCTAssertEqual(build(snap, "lab", at: "2026-09-24T15:47:10Z").message, "No usage data yet")
    }

    func testPaused() throws {
        var snap = try snapshot("paused")
        snap.profiles[0].updatedAt = Fixtures.date("2026-09-24T17:19:40Z")
        let d = build(snap, "work", at: "2026-09-24T17:20:00Z")
        XCTAssertEqual(d.state, .ok)
        XCTAssertTrue(d.isPaused)
        XCTAssertEqual(WidgetDisplay.pausedBadge, "⏸ Paused")
        XCTAssertEqual(d.resumeText, "resumes 20:00 · in 40m")
        XCTAssertEqual(d.supervisorLine, "2 ccs sessions · 2 paused · 1 other")
        XCTAssertEqual(d.session?.level, .red)
    }

    func testSingularSessionAndNoWarmup() throws {
        let d = build(try snapshot("offline"), "work", at: "2026-09-24T15:21:00Z")
        XCTAssertEqual(d.supervisorLine, "1 ccs session · 0 other")
        XCTAssertNil(d.nextWarmupText)
    }

    // MARK: - Rows, toggles, families

    func testExtraUsageRowShowsDetail() throws {
        let d = build(try snapshot("extra_usage"), "personal", at: "2026-09-24T15:47:10Z")
        let extra = try XCTUnwrap(d.rows.first { $0.kind == .extraUsage })
        XCTAssertEqual(extra.trailing, "€3.20 / €10.00")
        XCTAssertEqual(extra.percentText, "32%")
    }

    func testToggles() throws {
        let snap = try snapshot("spill")
        let all = build(snap, "personal", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(all.rows.map(\.kind), [.session, .weekly, .modelScoped, .extraUsage])

        let noWeekly = build(snap, "personal", at: "2026-09-24T15:47:10Z", options: WidgetOptions(showWeekly: false))
        XCTAssertEqual(noWeekly.rows.map(\.kind), [.session, .modelScoped, .extraUsage])
        XCTAssertNotNil(noWeekly.weeklyLine, "small always shows the weekly footer")

        let noModel = build(snap, "personal", at: "2026-09-24T15:47:10Z", options: WidgetOptions(showModelScoped: false))
        XCTAssertEqual(noModel.rows.map(\.kind), [.session, .weekly, .extraUsage])

        let noExtra = build(snap, "personal", at: "2026-09-24T15:47:10Z", options: WidgetOptions(showExtraUsage: false))
        XCTAssertEqual(noExtra.rows.map(\.kind), [.session, .weekly, .modelScoped])

        let onlySession = build(
            snap, "personal", at: "2026-09-24T15:47:10Z",
            options: WidgetOptions(showWeekly: false, showModelScoped: false, showExtraUsage: false)
        )
        XCTAssertEqual(onlySession.rows.map(\.kind), [.session])
    }

    func testRowsPerFamilyAndDropPriority() throws {
        var snap = try snapshot("spill")
        let reset = Fixtures.date("2026-09-26T06:00:00Z")
        snap.profiles[0].rows.insert(UsageRow(kind: .modelScoped, label: "Opus", percent: 20, level: .green, resetsAt: reset), at: 3)
        snap.profiles[0].rows.insert(UsageRow(kind: .modelScoped, label: "Sonnet", percent: 30, level: .green, resetsAt: reset), at: 4)
        let d = build(snap, "personal", at: "2026-09-24T15:47:10Z")
        XCTAssertEqual(d.rows.map(\.label), ["Session", "Weekly", "Fable", "Opus", "Sonnet", "Extra usage"])

        XCTAssertEqual(d.rows(for: .small).map(\.label), ["Session"])
        // medium: 4 rows; drop extra first, then the last model-scoped row.
        XCTAssertEqual(d.rows(for: .medium).map(\.label), ["Session", "Weekly", "Fable", "Opus"])
        XCTAssertEqual(d.rows(for: .large).map(\.label), ["Session", "Weekly", "Fable", "Opus", "Sonnet", "Extra usage"])

        // Only weekly left to drop after extra and model-scoped are gone.
        var many = WidgetDisplay(
            state: .ok, profileID: "x", emoji: "", name: "X", isPaused: false, resumeText: nil,
            session: nil, weeklyLine: nil, rows: [], supervisorLine: nil, nextWarmupText: nil,
            updatedText: nil, message: nil, footer: nil, dimmed: false, url: nil
        )
        func row(_ kind: RowKind, _ label: String) -> WidgetRowDisplay {
            WidgetRowDisplay(id: label, kind: kind, label: label, percent: 1, percentText: "1%", level: .green, trailing: nil)
        }
        many.rows = [row(.session, "S"), row(.weekly, "W1"), row(.weekly, "W2"), row(.weekly, "W3"), row(.extraUsage, "E"), row(.weekly, "W4")]
        XCTAssertEqual(many.rows(for: .medium).map(\.label), ["S", "W1", "W2", "W3"])
    }

    func testSessionRowOrderedFirst() throws {
        var snap = try snapshot("ok")
        snap.profiles[0].rows.reverse()
        let d = build(snap, "work", at: "2026-09-24T15:47:00Z")
        XCTAssertEqual(d.rows.first?.kind, .session)
    }

    // MARK: - Timeline

    func testEntryDatesPerMinute() {
        let now = Fixtures.date("2026-09-24T12:00:10Z")
        let dates = WidgetTimeline.entryDates(now: now)
        XCTAssertEqual(dates.count, 60)
        XCTAssertEqual(dates[0], now)
        XCTAssertEqual(dates[1], Fixtures.date("2026-09-24T12:01:00Z"))
        for i in 2 ..< dates.count {
            XCTAssertEqual(dates[i].timeIntervalSince(dates[i - 1]), 60)
        }
        XCTAssertEqual(dates.last, Fixtures.date("2026-09-24T12:59:00Z"))
        XCTAssertTrue(WidgetTimeline.entryDates(now: now, count: 0).isEmpty)
    }

    func testRelativeStringsCountDownAcrossEntries() throws {
        var snap = try snapshot("ok")
        let now = Fixtures.date("2026-09-24T12:00:10Z")
        snap.generatedAt = now
        snap.profiles[0].updatedAt = now
        // :20 (not :30): the clock time rounds to the nearest minute, so :30 would show 14:06.
        snap.profiles[0].rows[0].resetsAt = Fixtures.date("2026-09-24T12:05:20Z")
        let items = WidgetTimeline.items(snapshot: snap, profileID: "work", now: now, timeZone: tz)
        XCTAssertEqual(items.count, 60)
        let trailing = items.prefix(8).map { $0.display.session?.trailing ?? "" }
        XCTAssertEqual(trailing, [
            "14:05 · in 5m",
            "14:05 · in 4m",
            "14:05 · in 3m",
            "14:05 · in 2m",
            "14:05 · in 1m",
            "14:05 · in <1m",
            "14:05 · now",
            "14:05 · now",
        ])
        // Offline is judged at load: later entries stay ok while data is < 10 min old…
        XCTAssertEqual(items[8].display.state, .ok)
        // …and turn stale once `updated_at` is older than 10 min.
        XCTAssertEqual(items[11].display.state, .stale)
        XCTAssertEqual(items[11].display.footer, "updated 10m ago")
    }

    func testTimelineMatchesSharedTimeVectors() throws {
        struct Vector: Decodable {
            let name: String
            let tz: String
            let now: String
            let reset: String
            let compact: String
        }
        let vectors = try JSONDecoder().decode([Vector].self, from: try Fixtures.data("time_format.json"))
        var snap = try snapshot("ok")
        for v in vectors {
            let now = Fixtures.date(v.now)
            snap.generatedAt = now
            snap.profiles[0].updatedAt = now
            snap.profiles[0].rows[0].resetsAt = Fixtures.date(v.reset)
            let zone = try XCTUnwrap(TimeZone(identifier: v.tz))
            let item = try XCTUnwrap(WidgetTimeline.items(snapshot: snap, profileID: "work", now: now, count: 1, timeZone: zone).first)
            XCTAssertEqual(item.display.session?.trailing, v.compact, v.name)
        }
    }

    // MARK: - Loader and catalog

    func testLoaderDecodesEveryFixture() throws {
        for file in try Fixtures.files(in: "snapshot") {
            switch SnapshotLoader.load(from: file) {
            case .success(let snap):
                XCTAssertFalse(snap.profiles.isEmpty, file.lastPathComponent)
            case .failure(let error):
                XCTFail("\(file.lastPathComponent): \(error)")
            }
        }
    }

    func testLoaderErrors() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("p12-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        XCTAssertEqual(SnapshotLoader.load(from: dir.appendingPathComponent("missing.json")).failure, .missing)

        let corrupt = dir.appendingPathComponent("corrupt.json")
        try Data("{not json".utf8).write(to: corrupt)
        guard case .decode = SnapshotLoader.load(from: corrupt).failure else {
            return XCTFail("corrupt file should be a decode error")
        }

        let locked = dir.appendingPathComponent("locked.json")
        try Fixtures.data("snapshot/ok.json").write(to: locked)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: locked.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: locked.path) }
        guard case .unreadable = SnapshotLoader.load(from: locked).failure else {
            return XCTFail("EACCES should be unreadable")
        }
    }

    func testLoaderClassifiesSandboxDenial() {
        let eperm = NSError(domain: NSCocoaErrorDomain, code: CocoaError.fileReadNoPermission.rawValue, userInfo: [
            NSUnderlyingErrorKey: NSError(domain: NSPOSIXErrorDomain, code: Int(EPERM)),
        ])
        XCTAssertEqual(SnapshotLoader.classify(eperm), .sandboxDenied)
        let enoent = NSError(domain: NSPOSIXErrorDomain, code: Int(ENOENT))
        XCTAssertEqual(SnapshotLoader.classify(enoent), .missing)
    }

    func testProfileCatalog() throws {
        let snap = try snapshot("ok")
        XCTAssertEqual(WidgetProfileCatalog.choices(snap).map(\.id), ["work", "personal"])
        XCTAssertEqual(WidgetProfileCatalog.choices(snap).first?.title, "💼 Work")
        XCTAssertEqual(WidgetProfileCatalog.defaultChoice(snap)?.id, "work")
        XCTAssertEqual(WidgetProfileCatalog.choices(for: ["personal", "ghost"], in: snap).map(\.id), ["personal"])
        XCTAssertTrue(WidgetProfileCatalog.choices(nil).isEmpty)
        XCTAssertNil(WidgetProfileCatalog.defaultChoice(nil))
    }

    func testSamplesRenderEveryState() {
        let now = Fixtures.date("2026-09-24T12:00:00Z")
        let ok = WidgetDisplayBuilder.build(snapshot: WidgetSamples.snapshot(now: now), profileID: WidgetSamples.profileID, now: now)
        XCTAssertEqual(ok.state, .ok)
        XCTAssertEqual(ok.rows.count, 4)
        let paused = WidgetDisplayBuilder.build(
            snapshot: WidgetSamples.snapshot(now: now, paused: true), profileID: WidgetSamples.profileID, now: now
        )
        XCTAssertTrue(paused.isPaused)
        XCTAssertNotNil(paused.resumeText)
        let signIn = WidgetDisplayBuilder.build(
            snapshot: WidgetSamples.snapshot(now: now, status: .needsSignIn), profileID: WidgetSamples.profileID, now: now
        )
        XCTAssertEqual(signIn.state, .needsSignIn)
        let stale = WidgetDisplayBuilder.build(
            snapshot: WidgetSamples.snapshot(now: now, status: .stale), profileID: WidgetSamples.profileID, now: now
        )
        XCTAssertEqual(stale.state, .stale)
    }
}

private extension Result {
    var failure: Failure? {
        if case .failure(let error) = self { return error }
        return nil
    }
}

final class SmallWidgetStyleTests: XCTestCase {
    func testStyleParsing() {
        XCTAssertEqual(SmallWidgetStyle(configValue: nil), .bar)
        XCTAssertEqual(SmallWidgetStyle(configValue: "gauge"), .gauge)
        XCTAssertEqual(SmallWidgetStyle(configValue: "bar"), .bar)
        XCTAssertEqual(SmallWidgetStyle(configValue: "weird"), .bar)
        XCTAssertEqual(SmallWidgetStyle.allCases.map(\.title), ["Progress bar", "Gauge"])
    }

    func testGaugeGeometry() {
        XCTAssertEqual(GaugeGeometry.fraction(percent: -5), 0)
        XCTAssertEqual(GaugeGeometry.fraction(percent: 45), 0.45, accuracy: 1e-9)
        XCTAssertEqual(GaugeGeometry.fraction(percent: 140), 1)
        XCTAssertEqual(GaugeGeometry.needleDegrees(percent: 0), 180)
        XCTAssertEqual(GaugeGeometry.needleDegrees(percent: 50), 270)
        XCTAssertEqual(GaugeGeometry.needleDegrees(percent: 100), 360)
        let center = CGPoint(x: 50, y: 50)
        let left = GaugeGeometry.needleTip(center: center, length: 10, percent: 0)
        let top = GaugeGeometry.needleTip(center: center, length: 10, percent: 50)
        let right = GaugeGeometry.needleTip(center: center, length: 10, percent: 100)
        XCTAssertEqual(left.x, 40, accuracy: 1e-9); XCTAssertEqual(left.y, 50, accuracy: 1e-9)
        XCTAssertEqual(top.x, 50, accuracy: 1e-9); XCTAssertEqual(top.y, 40, accuracy: 1e-9)   // y-down: up
        XCTAssertEqual(right.x, 60, accuracy: 1e-9); XCTAssertEqual(right.y, 50, accuracy: 1e-9)
    }
}
