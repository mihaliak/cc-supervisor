@testable import CCSupervisor
import XCTest

@MainActor
final class AppModelTests: XCTestCase {
    private var dir: URL!
    private var file: URL!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-app-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        file = dir.appendingPathComponent("config.json")
        try Fixtures.data("config/python_written.json").write(to: file)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// Never a real `ccs`: a missing binary makes every action fail at once.
    private func model() -> AppModel {
        AppModel(
            config: AppConfig(ccsPath: dir.appendingPathComponent("no-ccs").path, menuBarMode: .letterPercent, fileExists: true),
            snapshotStore: SnapshotStore(fileURL: dir.appendingPathComponent("snapshot.json")),
            socketPath: dir.appendingPathComponent("daemon.sock").path
        )
    }

    private func disk(_ path: String) throws -> JSONValue? {
        try JSONValue.parse(try Data(contentsOf: file)).value(at: FieldPath.parse(path))
    }

    func testSignInPromptNamesTheProfile() {
        let known = SignInPrompt(profileID: "work", profile: ProfileSnapshot(id: "work", name: "Work", emoji: "💼", status: .needsSignIn))
        XCTAssertEqual(known.title, "Sign in to 💼 Work?")
        XCTAssertTrue(known.message.contains("“work”"))
        XCTAssertTrue(known.message.contains("opens your browser"))
        XCTAssertEqual(SignInPrompt(profileID: "lab", profile: nil).title, "Sign in to lab?")
    }

    /// ADR-0022: a `signin` link only asks; cancelling does nothing.
    func testSignInLinkAsksFirst() async throws {
        let model = model()
        let prompts = Recorder()
        model.confirmSignIn = { prompts.items.append($0.title); return false }
        model.open(DeepLink.signIn("work").url)
        XCTAssertEqual(prompts.items, ["Sign in to work?"])
        XCTAssertFalse(model.isBusy("signin:work"), "cancel starts nothing")
        XCTAssertNil(model.message(for: "work"))

        model.confirmSignIn = { prompts.items.append($0.title); return true }
        model.open(DeepLink.signIn("work").url)
        XCTAssertEqual(prompts.items.count, 2)
        XCTAssertTrue(model.isBusy("signin:work"), "confirmed: the sign-in starts")
        for _ in 0..<100 where model.isBusy("signin:work") {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(model.message(for: "work")?.isError, true, "the fake ccs path doesn't exist")
    }

    func testOtherLinksDontAsk() {
        let model = model()
        model.confirmSignIn = { _ in XCTFail("no prompt expected"); return false }
        model.open(DeepLink.profile("work").url)
        XCTAssertEqual(model.selectedProfileID, "work")
    }

    /// Quitting saves pending edits and a typed-but-uncommitted field value synchronously.
    func testQuitSavesPendingEditsAndTypedDrafts() throws {
        let model = model()
        let store = ConfigStore(fileURL: file)
        store.saveDelay = .seconds(60)
        let settings = SettingsController(app: model, store: store)
        store.set(.profile("work", "limits.session.pause"), .int(93))
        let name = store.stringBinding(.profile("personal", "name"))
        UncommittedDrafts.shared.set(UUID()) { name.wrappedValue = "Typed" }

        model.prepareToQuit()

        XCTAssertEqual(try disk("profiles[0].limits.session.pause"), .int(93))
        XCTAssertEqual(try disk("profiles[1].name"), .string("Typed"))
        XCTAssertEqual(try disk("revision"), .int(8), "one write")
        XCTAssertTrue(store.pending.isEmpty)
        XCTAssertEqual(UncommittedDrafts.shared.count, 0)
        _ = settings
    }

    func testDraftsCommitOnceAndCanBeWithdrawn() {
        let committed = Recorder()
        let kept = UUID()
        let withdrawn = UUID()
        UncommittedDrafts.shared.set(kept) { committed.items.append("kept") }
        UncommittedDrafts.shared.set(withdrawn) { committed.items.append("withdrawn") }
        UncommittedDrafts.shared.remove(withdrawn)
        UncommittedDrafts.shared.commitAll()
        UncommittedDrafts.shared.commitAll()
        XCTAssertEqual(committed.items, ["kept"])
    }
}

/// Collects values from closures (all on the main actor).
@MainActor
private final class Recorder {
    var items: [String] = []
}
