@testable import CCSupervisor
import XCTest

final class SnapshotDecodingTests: XCTestCase {
    func testEveryFixtureDecodes() throws {
        let files = try Fixtures.files(in: "snapshot")
        XCTAssertGreaterThanOrEqual(files.count, 6)
        for file in files {
            let snapshot = try SnapshotDecoding.decode(try Data(contentsOf: file))
            XCTAssertEqual(snapshot.schema, 1, file.lastPathComponent)
            XCTAssertNotNil(snapshot.generatedAt, file.lastPathComponent)
            XCTAssertFalse(snapshot.profiles.isEmpty, file.lastPathComponent)
        }
    }

    func testOkFixtureFields() throws {
        let snapshot = try SnapshotDecoding.decode(try Fixtures.data("snapshot/ok.json"))
        XCTAssertEqual(snapshot.profiles.map(\.id), ["work", "personal"])
        let work = try XCTUnwrap(snapshot.profile(id: "work"))
        XCTAssertEqual(work.emoji, "💼")
        XCTAssertEqual(work.status, .ok)
        XCTAssertEqual(work.level, .yellow)
        XCTAssertEqual(work.rows.map(\.kind), [.session, .weekly, .modelScoped])
        XCTAssertEqual(work.sessionRow?.percent, 45)
        XCTAssertEqual(work.sessionRow?.resetsAt, Fixtures.date("2026-09-24T18:00:00Z"))
        XCTAssertEqual(work.rows[2].label, "Fable")
        XCTAssertEqual(work.supervisor.activeSessions, 2)
        XCTAssertEqual(work.supervisor.otherSessions, 3)
        XCTAssertEqual(work.supervisor.nextWarmupAt, Fixtures.date("2026-09-25T04:00:00Z"))
        XCTAssertNil(work.supervisor.resumeAt)
        XCTAssertEqual(snapshot.worstLevel, .yellow)
    }

    func testStatesAndSupervisor() throws {
        let stale = try SnapshotDecoding.decode(try Fixtures.data("snapshot/stale.json")).profiles[0]
        XCTAssertEqual(stale.status, .stale)
        XCTAssertTrue(stale.stale)

        let signIn = try SnapshotDecoding.decode(try Fixtures.data("snapshot/needs_sign_in.json")).profiles[0]
        XCTAssertEqual(signIn.status, .needsSignIn)
        XCTAssertNil(signIn.level)
        XCTAssertNil(signIn.updatedAt)
        XCTAssertTrue(signIn.rows.isEmpty)

        let paused = try SnapshotDecoding.decode(try Fixtures.data("snapshot/paused.json")).profiles[0]
        XCTAssertEqual(paused.supervisor.state, .paused)
        XCTAssertEqual(paused.supervisor.pausedSessions, 2)
        XCTAssertEqual(paused.supervisor.resumeAt, Fixtures.date("2026-09-24T18:00:00Z"))

        let extra = try SnapshotDecoding.decode(try Fixtures.data("snapshot/extra_usage.json")).profiles[0]
        let row = try XCTUnwrap(extra.rows.first { $0.kind == .extraUsage })
        XCTAssertEqual(row.detail, "€3.20 / €10.00")
        XCTAssertNil(row.resetsAt)

        let noScoped = try SnapshotDecoding.decode(try Fixtures.data("snapshot/no_model_scoped.json")).profiles[0]
        XCTAssertFalse(noScoped.rows.contains { $0.kind == .modelScoped })
        XCTAssertEqual(noScoped.supervisor.state, .warned)
    }

    func testLenientDecoding() throws {
        let json = """
        {"schema":1,"generated_at":"2026-09-24T15:47:10.123Z","future_field":true,
         "profiles":[{"id":"x","status":"brand_new","level":"purple",
                      "rows":[{"kind":"mystery","percent":5}],"supervisor":{"state":"??"}}]}
        """
        let snapshot = try SnapshotDecoding.decode(Data(json.utf8))
        XCTAssertNotNil(snapshot.generatedAt)
        let p = snapshot.profiles[0]
        XCTAssertEqual(p.name, "x")
        XCTAssertEqual(p.status, .unknown)
        XCTAssertNil(p.level)
        XCTAssertEqual(p.rows[0].kind, .unknown)
        XCTAssertEqual(p.supervisor.state, .unknown)
        XCTAssertEqual(p.supervisor.activeSessions, 0)
    }

    /// Regression: one malformed profile (or row) dropped every profile, so the menu said
    /// "No profiles yet." and every widget showed "Choose a profile".
    func testOneBadProfileDoesNotHideTheOthers() throws {
        let json = """
        {"schema":1,"generated_at":"2026-09-24T15:47:10Z","profiles":[
          {"id":"work","name":"Work","emoji":"💼","status":"ok","rows":[]},
          {"name":"no id"},
          42,
          {"id":"personal","rows":[7,{"kind":"session","label":"Session","percent":5}]}]}
        """
        let snapshot = try SnapshotDecoding.decode(Data(json.utf8))
        XCTAssertEqual(snapshot.profiles.map(\.id), ["work", "personal"])
        XCTAssertEqual(snapshot.profile(id: "personal")?.rows.map(\.percent), [5])
    }

    func testDaemonOfflineAfterFiveMinutes() throws {
        let snapshot = try SnapshotDecoding.decode(try Fixtures.data("snapshot/ok.json"))
        let generated = try XCTUnwrap(snapshot.generatedAt)
        XCTAssertFalse(snapshot.isDaemonOffline(now: generated.addingTimeInterval(299)))
        XCTAssertTrue(snapshot.isDaemonOffline(now: generated.addingTimeInterval(301)))
        XCTAssertTrue(WidgetSnapshot(generatedAt: nil, profiles: []).isDaemonOffline(now: .now))
    }

    func testMenuLabelParts() throws {
        let snapshot = try SnapshotDecoding.decode(try Fixtures.data("snapshot/ok.json"))
        let profile = snapshot.profiles[0]
        XCTAssertEqual(profile.initial, String(profile.name.prefix(1)).uppercased())
        XCTAssertEqual(MenuLabelContent.percentText(profile.sessionRow, profile: profile), "45%")
        XCTAssertEqual(MenuLabelContent.dotLevel(profile.sessionRow, profile: profile), profile.sessionRow?.level)
        let weekly = try XCTUnwrap(profile.weeklyRow)
        XCTAssertEqual(MenuLabelContent.percentText(weekly, profile: profile), "\(weekly.percent)%")
        XCTAssertEqual(MenuLabelContent.dotLevel(weekly, profile: profile), weekly.level)

        let signIn = try SnapshotDecoding.decode(try Fixtures.data("snapshot/needs_sign_in.json")).profiles[0]
        XCTAssertEqual(MenuLabelContent.percentText(signIn.sessionRow, profile: signIn), "?%")
        XCTAssertNil(MenuLabelContent.dotLevel(signIn.sessionRow, profile: signIn))
        XCTAssertTrue(MenuLabelContent.accessibilityText(snapshot).contains("session 45%"))
    }

    func testProfileInitial() {
        let json = #"{"generated_at":null,"profiles":[{"id":"work","name":"work"},{"id":"x9","name":" "}]}"#
        let snapshot = try? SnapshotDecoding.decode(Data(json.utf8))
        XCTAssertEqual(snapshot?.profiles.map(\.initial), ["W", "X"])
    }

    /// Regression: the menu card rendered `"\(emoji) \(name)"`, a leading space when the
    /// (optional) emoji is empty. Menu cards, widgets and pickers share one title rule.
    func testProfileTitleSkipsEmptyParts() {
        XCTAssertEqual(ProfileSnapshot(id: "work", name: "Work", emoji: "💼", status: .ok).title, "💼 Work")
        XCTAssertEqual(ProfileSnapshot(id: "work", name: "Work", emoji: "", status: .ok).title, "Work")
        XCTAssertEqual(ProfileSnapshot(id: "work", name: "Work", emoji: " ", status: .ok).title, "Work")
        XCTAssertEqual(ProfileTitle.text(emoji: "💼", name: ""), "💼")
    }

    func testStatusChips() throws {
        let now = Fixtures.date("2026-09-24T15:47:00Z")
        let ok = try SnapshotDecoding.decode(try Fixtures.data("snapshot/ok.json")).profiles[0]
        XCTAssertNil(StatusChip.make(for: ok, now: now))
        let stale = try SnapshotDecoding.decode(try Fixtures.data("snapshot/stale.json")).profiles[0]
        XCTAssertEqual(StatusChip.make(for: stale, now: now)?.text, "updated 14m ago")
        let signIn = try SnapshotDecoding.decode(try Fixtures.data("snapshot/needs_sign_in.json")).profiles[0]
        XCTAssertEqual(StatusChip.make(for: signIn, now: now)?.offersSignIn, true)
    }

    func testUsageRowTrailingText() throws {
        let now = Fixtures.date("2026-09-24T15:47:00Z")
        let extra = UsageRow(kind: .extraUsage, label: "Extra usage", percent: 32, level: .green, detail: "€3.20 / €10.00")
        XCTAssertEqual(UsageRowView.trailingText(extra, now: now), "€3.20 / €10.00")
        let session = UsageRow(kind: .session, label: "Session", percent: 45, level: .green, resetsAt: Fixtures.date("2026-09-24T18:00:00Z"))
        let text = try XCTUnwrap(UsageRowView.trailingText(session, now: now))
        XCTAssertTrue(text.hasSuffix(" · in 2h 13m"), text)
    }
}
