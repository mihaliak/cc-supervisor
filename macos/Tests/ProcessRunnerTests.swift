@testable import CCSupervisor
import Darwin
import XCTest

final class ProcessRunnerTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("ccs-runner-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: dir)
    }

    private func script(_ body: String) throws -> URL {
        let url = dir.appendingPathComponent("child.sh")
        try ("#!/bin/sh\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url
    }

    private func waitForFile(_ url: URL) async throws {
        for _ in 0..<250 where !FileManager.default.fileExists(atPath: url.path) {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path), "\(url.lastPathComponent) never appeared")
    }

    private func signals(_ url: URL) -> [String] {
        ((try? String(contentsOf: url, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
    }

    /// Regression: the reader group was entered after `run()`, so a child that exited at
    /// once could finish before it and its output was returned empty.
    func testFastChildOutputIsNeverLost() async throws {
        for i in 0..<100 {
            let result = try await ProcessRunner.run(
                executable: URL(fileURLWithPath: "/bin/echo"),
                arguments: ["line \(i)"],
                environment: [:],
                timeout: 10
            )
            XCTAssertEqual(String(decoding: result.stdout, as: UTF8.self), "line \(i)\n")
        }
    }

    /// Cancelling sends SIGINT first: `ccs` handles it like Ctrl-C and stops the
    /// `claude auth login` process group, which SIGTERM would leave orphaned.
    func testCancelSendsSIGINTFirst() async throws {
        let marker = dir.appendingPathComponent("signals")
        let started = dir.appendingPathComponent("started")
        let url = try script("""
        trap 'echo INT >> "\(marker.path)"; exit 130' INT
        trap 'echo TERM >> "\(marker.path)"; exit 143' TERM
        touch "\(started.path)"
        while :; do sleep 0.05; done
        """)
        let task = Task {
            try await ProcessRunner.run(executable: url, arguments: [], environment: [:], timeout: 60, grace: 5)
        }
        try await waitForFile(started)
        let begin = Date()
        task.cancel()
        let result = try await task.value
        XCTAssertEqual(signals(marker), ["INT"])
        XCTAssertEqual(result.status, 130)
        XCTAssertFalse(result.timedOut)
        XCTAssertLessThan(Date().timeIntervalSince(begin), 4, "exited on SIGINT, no escalation")
    }

    /// A child that ignores SIGINT gets SIGTERM after the grace period, and one that survives
    /// that too gets SIGKILL after another.
    func testEscalatesToSIGTERMThenSIGKILL() async throws {
        let marker = dir.appendingPathComponent("signals")
        let started = dir.appendingPathComponent("started")
        let url = try script("""
        trap '' INT
        trap 'echo TERM >> "\(marker.path)"' TERM
        touch "\(started.path)"
        while :; do sleep 0.05; done
        """)
        let task = Task {
            try await ProcessRunner.run(executable: url, arguments: [], environment: [:], timeout: 60, grace: 0.4)
        }
        try await waitForFile(started)
        let begin = Date()
        task.cancel()
        let result = try await task.value
        let elapsed = Date().timeIntervalSince(begin)
        XCTAssertEqual(signals(marker), ["TERM"], "SIGINT ignored, SIGTERM trapped")
        XCTAssertEqual(result.status, SIGKILL, "killed by the last step")
        XCTAssertGreaterThanOrEqual(elapsed, 0.8, "two grace periods")
        XCTAssertLessThan(elapsed, 8)
    }
}
