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
    private var dir: String!

    /// A private (0700) state dir; short, since socket paths are limited to 104 bytes.
    override func setUpWithError() throws {
        dir = "/tmp/ccs-app-test-\(getpid())-\(Int.random(in: 1000...9999))"
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(atPath: dir)
    }

    func testHelloSubscribeAndPushes() async throws {
        let path = dir + "/daemon.sock"
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

    /// Regression: `reconnectNow()` while the first `hello` reply was pending kept counting
    /// request ids, so the second `hello` went out as id 3 and the app never went online.
    func testReconnectNowStartsIdsOver() async throws {
        let path = dir + "/daemon.sock"
        let server = try TestSocketServer(path: path)
        defer { server.close() }

        let connection = DaemonConnection(socketPath: path, appVersion: "9.9")
        connection.start()
        defer { connection.stop() }

        // First connection: read hello + subscribe, never answer (and keep it open).
        let first = try server.acceptAndServe { $0.count >= 2 } responses: { _ in [] }
        XCTAssertEqual(Self.ids(first), [1, 2])

        connection.reconnectNow()
        let second = try server.acceptAndServe { $0.count >= 2 } responses: { lines in
            Self.ids(lines).map { #"{"proto":1,"id":\#($0),"ok":true}"# }
        }
        XCTAssertEqual(Self.ids(second), [1, 2], "a new connection starts at id 1")

        let updates = connection.updates
        let collector = Task { () -> Bool in
            for await update in updates {
                if case .online(true) = update { return true }
            }
            return false
        }
        let watchdog = Task {
            try await Task.sleep(for: .seconds(5))
            collector.cancel()
        }
        let online = await collector.value
        watchdog.cancel()
        XCTAssertTrue(online, "the reply to the second hello brings the app online")
    }

    /// ADR-0022: only a socket we own, in a dir we own that nobody else can write to.
    func testSocketTrust() throws {
        let path = dir + "/daemon.sock"
        XCTAssertNil(DaemonConnection.socketTrustProblem(path), "missing: connecting fails as usual")

        let server = try TestSocketServer(path: path)
        defer { server.close() }
        XCTAssertNil(DaemonConnection.socketTrustProblem(path), "0700 dir + our socket")
        XCTAssertEqual(
            DaemonConnection.socketTrustProblem(path, uid: getuid() + 1),
            "\(dir!) is not owned by the current user"
        )

        chmod(dir!, 0o777)
        XCTAssertEqual(DaemonConnection.socketTrustProblem(path), "\(dir!) is group- or world-writable")
        chmod(dir!, 0o720)
        XCTAssertEqual(DaemonConnection.socketTrustProblem(path), "\(dir!) is group- or world-writable")
        chmod(dir!, 0o700)

        let plain = dir + "/plain"
        FileManager.default.createFile(atPath: plain, contents: Data())
        XCTAssertEqual(DaemonConnection.socketTrustProblem(plain), "\(plain) is not a socket")
        let link = dir + "/link.sock"
        try FileManager.default.createSymbolicLink(atPath: link, withDestinationPath: path)
        XCTAssertEqual(DaemonConnection.socketTrustProblem(link), "\(link) is not a socket", "a symlink is refused")
        XCTAssertEqual(DaemonConnection.socketTrustProblem("/etc/hosts"), "/etc is not owned by the current user")
    }

    /// A refused socket is never connected to: the app stays offline.
    func testRefusedSocketStaysOffline() async throws {
        let path = dir + "/daemon.sock"
        let server = try TestSocketServer(path: path)
        defer { server.close() }
        chmod(dir!, 0o777)
        defer { chmod(dir!, 0o700) }

        let connection = DaemonConnection(socketPath: path, appVersion: "9.9")
        connection.start()
        defer { connection.stop() }
        XCTAssertFalse(server.hasPendingClient(within: 0.5), "no connection attempt")
    }

    private static func ids(_ lines: [String]) -> [Int] {
        lines.compactMap { ((try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any])?["id"] as? Int }
    }
}

/// Minimal blocking unix-socket server for the tests above.
private final class TestSocketServer: @unchecked Sendable {
    private let fd: Int32
    private let path: String
    private var clients: [Int32] = []

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
        guard bound == 0, listen(fd, 4) == 0 else { throw POSIXError(.EADDRINUSE) }
    }

    /// Accept one client, read lines until `enough(lines)`, then write `responses(lines)`.
    /// Clients stay open until `close()`.
    func acceptAndServe(
        until enough: ([String]) -> Bool,
        responses: ([String]) -> [String]
    ) throws -> [String] {
        let client = accept(fd, nil, nil)
        guard client >= 0 else { throw POSIXError(.ECONNABORTED) }
        clients.append(client)
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

    /// True when a client connects within `seconds` (it isn't accepted).
    func hasPendingClient(within seconds: Double) -> Bool {
        var fds = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        return poll(&fds, 1, Int32(seconds * 1000)) > 0
    }

    func close() {
        for client in clients { Darwin.close(client) }
        Darwin.close(fd)
        unlink(path)
    }
}
