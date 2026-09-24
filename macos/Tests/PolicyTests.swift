@testable import CCSupervisor
import UserNotifications
import XCTest

final class WidgetReloadPolicyTests: XCTestCase {
    private func snapshot(status: String = "ok", state: String = "normal", percent: Int = 10) throws -> WidgetSnapshot {
        let json = """
        {"schema":1,"generated_at":"2026-09-24T15:00:00Z","profiles":[{"id":"work","name":"Work","emoji":"💼",
         "status":"\(status)","stale":false,"level":"green","updated_at":null,
         "rows":[{"kind":"session","label":"Session","percent":\(percent),"level":"green","resets_at":null}],
         "supervisor":{"state":"\(state)","active_sessions":0,"paused_sessions":0,"other_sessions":0,
                       "resume_at":null,"next_warmup_at":null}}]}
        """
        return try SnapshotDecoding.decode(Data(json.utf8))
    }

    func testThrottleAndTrailing() throws {
        var policy = WidgetReloadPolicy()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertEqual(policy.onSnapshot(try snapshot(percent: 10), now: t0), .reloadNow)
        // Same status/state within 60 s → one trailing reload at t0 + 60.
        XCTAssertEqual(
            policy.onSnapshot(try snapshot(percent: 11), now: t0.addingTimeInterval(10)),
            .scheduleTrailing(at: t0.addingTimeInterval(60))
        )
        XCTAssertEqual(policy.onSnapshot(try snapshot(percent: 12), now: t0.addingTimeInterval(20)), .none)
        XCTAssertEqual(policy.onTrailingFired(now: t0.addingTimeInterval(60)), .reloadNow)
        XCTAssertEqual(policy.onTrailingFired(now: t0.addingTimeInterval(61)), .none)
        // After the window, a plain change reloads immediately.
        XCTAssertEqual(policy.onSnapshot(try snapshot(percent: 13), now: t0.addingTimeInterval(125)), .reloadNow)
    }

    func testImmediateOnStatusOrStateChange() throws {
        var policy = WidgetReloadPolicy()
        let t0 = Date(timeIntervalSince1970: 2_000_000)
        XCTAssertEqual(policy.onSnapshot(try snapshot(), now: t0), .reloadNow)
        XCTAssertEqual(policy.onSnapshot(try snapshot(state: "paused"), now: t0.addingTimeInterval(5)), .reloadNow)
        XCTAssertEqual(policy.onSnapshot(try snapshot(status: "needs_sign_in", state: "paused"), now: t0.addingTimeInterval(6)), .reloadNow)
        // A state change also cancels a pending trailing reload.
        XCTAssertEqual(
            policy.onSnapshot(try snapshot(status: "needs_sign_in", state: "paused", percent: 50), now: t0.addingTimeInterval(7)),
            .scheduleTrailing(at: t0.addingTimeInterval(66))
        )
        XCTAssertEqual(policy.onSnapshot(try snapshot(status: "ok", state: "paused"), now: t0.addingTimeInterval(8)), .reloadNow)
        XCTAssertFalse(policy.trailingPending)
    }

    @MainActor
    func testReloaderCallsReloadAll() throws {
        let counter = Counter()
        let reloader = WidgetReloader { counter.value += 1 }
        reloader.snapshotChanged(try snapshot())
        reloader.snapshotChanged(try snapshot(state: "paused"))
        XCTAssertEqual(counter.value, 2)
    }
}

@MainActor
private final class Counter {
    var value = 0
}

final class TriggerDebouncerTests: XCTestCase {
    func testOnePerWindow() {
        var d = TriggerDebouncer(window: 60)
        let t0 = Date(timeIntervalSince1970: 0)
        XCTAssertTrue(d.shouldFire(now: t0))
        XCTAssertFalse(d.shouldFire(now: t0.addingTimeInterval(30)))
        XCTAssertFalse(d.shouldFire(now: t0.addingTimeInterval(59)))
        XCTAssertTrue(d.shouldFire(now: t0.addingTimeInterval(60)))
    }
}

final class DeepLinkTests: XCTestCase {
    func testParse() throws {
        XCTAssertEqual(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://profile/work"))), .profile("work"))
        XCTAssertEqual(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://signin/my-work2"))), .signIn("my-work2"))
        XCTAssertEqual(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://refresh"))), .refresh)
        XCTAssertEqual(DeepLink(url: try XCTUnwrap(URL(string: "CCSupervisor://Refresh"))), .refresh)
        XCTAssertNil(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://profile"))))
        XCTAssertNil(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://profile/Bad_ID"))))
        XCTAssertNil(DeepLink(url: try XCTUnwrap(URL(string: "ccsupervisor://unknown/x"))))
        XCTAssertNil(DeepLink(url: try XCTUnwrap(URL(string: "https://profile/work"))))
    }

    func testRoundTrip() {
        for link in [DeepLink.profile("work"), .signIn("personal"), .refresh] {
            XCTAssertEqual(DeepLink(url: link.url), link)
        }
    }
}

final class NotificationRouterTests: XCTestCase {
    private func event(notify: Bool?, title: String? = "💼 Work: session at 82%", key: String? = "warn:work:session") -> DaemonEvent {
        DaemonEvent(
            ts: "2026-09-24T15:47:00Z",
            type: "limit.warn",
            profileId: "work",
            key: key,
            data: .init(title: title, body: "Resets 20:00 (in 2h 13m)", notify: notify)
        )
    }

    func testPostsVerbatimOnlyWhenNotify() throws {
        let request = try XCTUnwrap(NotificationRouter.request(for: event(notify: true)))
        XCTAssertEqual(request.identifier, "warn:work:session")
        XCTAssertEqual(request.content.title, "💼 Work: session at 82%")
        XCTAssertEqual(request.content.body, "Resets 20:00 (in 2h 13m)")
        XCTAssertEqual(request.content.threadIdentifier, "work")
        XCTAssertEqual(request.content.userInfo["url"] as? String, "ccsupervisor://profile/work")

        XCTAssertNil(NotificationRouter.request(for: event(notify: false)))
        XCTAssertNil(NotificationRouter.request(for: event(notify: nil)))
        XCTAssertNil(NotificationRouter.request(for: event(notify: true, title: "")))
        XCTAssertFalse(try XCTUnwrap(NotificationRouter.request(for: event(notify: true, key: nil))).identifier.isEmpty)
    }
}
