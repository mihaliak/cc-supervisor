@testable import CCSupervisor
import Darwin
import XCTest

final class JSONLinesFramerTests: XCTestCase {
    func testSplitsAndReassembles() {
        var framer = JSONLinesFramer()
        XCTAssertEqual(framer.append(Data(#"{"a":1}"#.utf8)), [])
        XCTAssertEqual(framer.pendingByteCount, 7)
        let lines = framer.append(Data("\n{\"b\":2}\n\n  \n{\"c\"".utf8))
        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, [#"{"a":1}"#, #"{"b":2}"#])
        XCTAssertEqual(framer.append(Data(":3}\r\n".utf8)).map { String(decoding: $0, as: UTF8.self) }, ["{\"c\":3}\r"])
        XCTAssertEqual(framer.pendingByteCount, 0)
    }

    func testMessageClassification() {
        XCTAssertEqual(DaemonMessage.parse(Data(#"{"proto":1,"id":1,"ok":true}"#.utf8)), .reply(id: 1, ok: true, error: nil))
        XCTAssertEqual(
            DaemonMessage.parse(Data(#"{"proto":1,"id":3,"ok":false,"error":"unknown_op"}"#.utf8)),
            .reply(id: 3, ok: false, error: "unknown_op")
        )
        XCTAssertEqual(DaemonMessage.parse(Data(#"{"proto":1,"cmd":{"type":"pause"}}"#.utf8)), .command)
        XCTAssertEqual(DaemonMessage.parse(Data("not json".utf8)), .unknown)
        if case .snapshot(let data) = DaemonMessage.parse(Data(#"{"proto":1,"snapshot":{"schema":1,"profiles":[]}}"#.utf8)) {
            XCTAssertNoThrow(try SnapshotDecoding.decode(data))
        } else {
            XCTFail("expected snapshot")
        }
        let line = #"{"proto":1,"event":{"schema":1,"ts":"t","type":"limit.warn","profile_id":"work","key":"k","data":{"title":"T","body":"B","notify":true}}}"#
        guard case .event(let event) = DaemonMessage.parse(Data(line.utf8)) else { return XCTFail("expected event") }
        XCTAssertEqual(event.type, "limit.warn")
        XCTAssertEqual(event.profileId, "work")
        XCTAssertEqual(event.data?.notify, true)
    }

    func testBackoff() {
        var b = ReconnectBackoff()
        XCTAssertEqual((0..<7).map { _ in b.nextDelay() }, [1, 2, 5, 10, 30, 30, 30])
        b.connected(for: 10)
        XCTAssertEqual(b.nextDelay(), 30)
        b.connected(for: 61)
        XCTAssertEqual(b.nextDelay(), 1)
    }

    func testSocketPathLimit() {
        XCTAssertTrue(DaemonConnection.isValidSocketPath("/Users/u/.local/state/ccs/daemon.sock"))
        XCTAssertFalse(DaemonConnection.isValidSocketPath("/" + String(repeating: "x", count: 110)))
        XCTAssertFalse(DaemonConnection.isValidSocketPath(""))
    }
}

/// End-to-end against a tiny POSIX unix-socket server speaking `schema/ipc.md`.
final class DaemonConnectionTests: XCTestCase {
    func testHelloSubscribeAndPushes() async throws {
        let path = "/tmp/ccs-app-test-\(getpid())-\(Int.random(in: 1000...9999)).sock"
        let server = try TestSocketServer(path: path)
        defer { server.close() }

        let connection = DaemonConnection(socketPath: path, appVersion: "9.9")
        connection.start()
        defer { connection.stop() }

        let received = try server.acceptAndServe { lines in
            // hello, subscribe → replies, then a snapshot and an event push.
            lines.count >= 2
        } responses: { lines in
            var out: [String] = []
            for line in lines {
                let obj = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] ?? [:]
                out.append(#"{"proto":1,"id":\#(obj["id"] as? Int ?? 0),"ok":true}"#)
            }
            out.append(#"{"proto":1,"snapshot":{"schema":1,"generated_at":"2026-09-24T15:00:00Z","profiles":[]}}"#)
            out.append(#"{"proto":1,"event":{"schema":1,"ts":"t","type":"limit.warn","profile_id":"work","key":"k","data":{"title":"T","body":"B","notify":true}}}"#)
            return out
        }

        let requests = received.compactMap { (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any] }
        XCTAssertEqual(requests.first?["op"] as? String, "hello")
        XCTAssertEqual(requests.first?["client"] as? String, "app")
        XCTAssertEqual(requests.first?["proto"] as? Int, 1)
        XCTAssertEqual(requests.dropFirst().first?["op"] as? String, "subscribe")
        XCTAssertEqual(requests.dropFirst().first?["topics"] as? [String], ["events", "snapshot"])

        let updates = connection.updates
        let collector = Task { () -> (Bool, Bool, Bool) in
            var sawOnline = false, sawSnapshot = false, sawEvent = false
            for await update in updates {
                switch update {
                case .online(true): sawOnline = true
                case .snapshot: sawSnapshot = true
                case .event(let e): sawEvent = e.type == "limit.warn"
                default: break
                }
                if sawOnline && sawSnapshot && sawEvent { break }
            }
            return (sawOnline, sawSnapshot, sawEvent)
        }
        let watchdog = Task {
            try await Task.sleep(for: .seconds(5))
            collector.cancel()
        }
        let (sawOnline, sawSnapshot, sawEvent) = await collector.value
        watchdog.cancel()
        XCTAssertTrue(sawOnline)
        XCTAssertTrue(sawSnapshot)
        XCTAssertTrue(sawEvent)
    }
}

/// Minimal blocking unix-socket server for the test above.
private final class TestSocketServer: @unchecked Sendable {
    private let fd: Int32
    private let path: String
    private var client: Int32 = -1

    init(path: String) throws {
        self.path = path
        unlink(path)
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.EIO) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            let bytes = Array(path.utf8)
            for (i, b) in bytes.enumerated() where i < raw.count - 1 { raw[i] = b }
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) }
        }
        guard bound == 0, listen(fd, 1) == 0 else { throw POSIXError(.EADDRINUSE) }
    }

    /// Accept one client, read lines until `enough(lines)`, then write `responses(lines)`.
    func acceptAndServe(
        until enough: ([String]) -> Bool,
        responses: ([String]) -> [String]
    ) throws -> [String] {
        client = accept(fd, nil, nil)
        guard client >= 0 else { throw POSIXError(.ECONNABORTED) }
        var tv = timeval(tv_sec: 5, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var buffer = Data()
        var lines: [String] = []
        var chunk = [UInt8](repeating: 0, count: 4096)
        while !enough(lines) {
            let n = read(client, &chunk, chunk.count)
            guard n > 0 else { break }
            buffer.append(contentsOf: chunk[0..<n])
            while let nl = buffer.firstIndex(of: 0x0A) {
                lines.append(String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self))
                buffer = Data(buffer[buffer.index(after: nl)...])
            }
        }
        let out = responses(lines).map { $0 + "\n" }.joined()
        _ = out.withCString { write(client, $0, strlen($0)) }
        return lines
    }

    func close() {
        if client >= 0 { Darwin.close(client) }
        Darwin.close(fd)
        unlink(path)
    }
}
