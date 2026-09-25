import Foundation
import Network
import os

/// Newline-delimited JSON framing with a reassembly buffer (`schema/ipc.md`).
struct JSONLinesFramer: Sendable {
    private var buffer = Data()

    /// Append received bytes; return every complete, non-blank line.
    mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var lines: [Data] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = Data(buffer[buffer.startIndex..<newline])
            buffer = Data(buffer[buffer.index(after: newline)...])
            if line.contains(where: { $0 != 0x20 && $0 != 0x09 && $0 != 0x0D }) {
                lines.append(line)
            }
        }
        return lines
    }

    var pendingByteCount: Int { buffer.count }

    mutating func reset() { buffer = Data() }
}

/// A daemon event line (`schema/event.schema.json`). Text is built in Python (ADR-0015).
struct DaemonEvent: Decodable, Sendable, Equatable {
    struct Payload: Decodable, Sendable, Equatable {
        var title: String?
        var body: String?
        var notify: Bool?
    }

    var ts: String?
    var type: String
    var profileId: String?
    var key: String?
    var data: Payload?

    enum CodingKeys: String, CodingKey {
        case ts, type, key, data
        case profileId = "profile_id"
    }
}

/// What the connection publishes to the app.
enum DaemonUpdate: Sendable {
    case online(Bool)
    case event(DaemonEvent)
    case snapshot(Data)
}

/// Classify one received line (pure; tested).
enum DaemonMessage: Equatable {
    case reply(id: Int, ok: Bool, error: String?)
    case event(DaemonEvent)
    case snapshot(Data)
    case command
    case unknown

    static func parse(_ line: Data) -> DaemonMessage {
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else {
            return .unknown
        }
        if let event = object["event"] as? [String: Any],
           let data = try? JSONSerialization.data(withJSONObject: event),
           let decoded = try? JSONDecoder().decode(DaemonEvent.self, from: data) {
            return .event(decoded)
        }
        if let snapshot = object["snapshot"] as? [String: Any],
           let data = try? JSONSerialization.data(withJSONObject: snapshot) {
            return .snapshot(data)
        }
        if object["cmd"] != nil { return .command }
        if let id = object["id"] as? Int {
            return .reply(id: id, ok: (object["ok"] as? Bool) ?? false, error: object["error"] as? String)
        }
        return .unknown
    }

    static func == (lhs: DaemonMessage, rhs: DaemonMessage) -> Bool {
        switch (lhs, rhs) {
        case let (.reply(a, b, c), .reply(x, y, z)): a == x && b == y && c == z
        case let (.event(a), .event(b)): a == b
        case let (.snapshot(a), .snapshot(b)): a == b
        case (.command, .command), (.unknown, .unknown): true
        default: false
        }
    }
}

/// Reconnect delays: 1, 2, 5, 10, 30 s (capped); reset after 60 s connected.
struct ReconnectBackoff: Sendable {
    static let steps: [TimeInterval] = [1, 2, 5, 10, 30]
    static let resetAfter: TimeInterval = 60
    private(set) var attempt = 0

    mutating func nextDelay() -> TimeInterval {
        defer { attempt += 1 }
        return Self.steps[min(attempt, Self.steps.count - 1)]
    }

    mutating func connected(for duration: TimeInterval) {
        if duration >= Self.resetAfter { attempt = 0 }
    }

    mutating func reset() { attempt = 0 }
}

/// Client of the daemon's unix socket (ADR-0005, `schema/ipc.md`): `hello`, then
/// `subscribe` to `events` + `snapshot`; reconnects with backoff. All mutable state
/// lives on `queue`.
final class DaemonConnection: @unchecked Sendable {
    /// Unix socket paths are limited to 104 bytes on macOS.
    static let maxSocketPathBytes = 103

    let socketPath: String
    let updates: AsyncStream<DaemonUpdate>

    private let continuation: AsyncStream<DaemonUpdate>.Continuation
    private let queue = DispatchQueue(label: "local.ccsupervisor.daemon-connection")
    private let log = Logger(subsystem: "local.ccsupervisor.app", category: "daemon")
    private let appVersion: String

    private var connection: NWConnection?
    private var framer = JSONLinesFramer()
    private var backoff = ReconnectBackoff()
    /// Request ids restart at 1 on every connection; `hello` is always the first request.
    private var nextID = 1
    private var helloID: Int?
    private var isOnline = false
    private var connectedAt: Date?
    private var stopped = true
    private var reconnectItem: DispatchWorkItem?

    init(socketPath: String, appVersion: String) {
        self.socketPath = socketPath
        self.appVersion = appVersion
        (updates, continuation) = AsyncStream.makeStream(of: DaemonUpdate.self, bufferingPolicy: .bufferingNewest(256))
    }

    static func isValidSocketPath(_ path: String) -> Bool {
        !path.isEmpty && path.utf8.count <= maxSocketPathBytes
    }

    /// Why the socket must not be used, or nil (ADR-0022; same rules as
    /// `ccs.daemon.client.socket_trust_error`). The socket (`lstat`: a symlink is refused) and
    /// its directory must belong to `uid`, and the directory must not be group- or
    /// world-writable. A missing path is not an error here: connecting then fails as usual.
    static func socketTrustProblem(_ path: String, uid: uid_t = getuid()) -> String? {
        let dir = (path as NSString).deletingLastPathComponent
        var link = stat()
        var folder = stat()
        var sock = stat()
        // A symlinked state dir is judged by its target, but the link must be ours too.
        guard lstat(dir, &link) == 0, stat(dir, &folder) == 0, lstat(path, &sock) == 0 else {
            let error = errno
            return error == ENOENT ? nil : "cannot inspect \(path): \(String(cString: strerror(error)))"
        }
        if link.st_uid != uid || folder.st_uid != uid {
            return "\(dir) is not owned by the current user"
        }
        if folder.st_mode & 0o022 != 0 {
            return "\(dir) is group- or world-writable"
        }
        if sock.st_mode & mode_t(S_IFMT) != mode_t(S_IFSOCK) {
            return "\(path) is not a socket"
        }
        if sock.st_uid != uid {
            return "\(path) is not owned by the current user"
        }
        return nil
    }

    func start() {
        queue.async {
            guard self.stopped else { return }
            self.stopped = false
            self.connect()
        }
    }

    func stop() {
        queue.async {
            self.stopped = true
            self.reconnectItem?.cancel()
            self.connection?.cancel()
            self.connection = nil
            self.setOnline(false)
        }
    }

    /// Reconnect right away (e.g. after `ccs daemon start`).
    func reconnectNow() {
        queue.async {
            guard !self.stopped, !self.isOnline else { return }
            self.reconnectItem?.cancel()
            self.backoff.reset()
            self.connection?.cancel()
            self.connection = nil
            self.connect()
        }
    }

    /// Fire-and-forget request, e.g. `send(op: "refresh")`.
    func send(op: String, fields: [String: String] = [:]) {
        queue.async {
            guard self.isOnline else { return }
            var message: [String: Any] = fields
            message["op"] = op
            self.write(message)
        }
    }

    // MARK: - Queue-confined

    private func connect() {
        guard !stopped else { return }
        guard Self.isValidSocketPath(socketPath) else {
            log.error("socket path too long (\(self.socketPath.utf8.count) bytes): \(self.socketPath, privacy: .public)")
            return
        }
        if let problem = Self.socketTrustProblem(socketPath) {
            // Another user could be listening there; stay offline and retry with backoff.
            log.error("refusing the daemon socket: \(problem, privacy: .public)")
            dropAndScheduleReconnect()
            return
        }
        framer.reset()
        // A reconnect (e.g. `reconnectNow()` while a `hello` reply was pending) starts a new
        // conversation; a leftover id would never match the `hello` reply and keep us offline.
        nextID = 1
        helloID = nil
        let conn = NWConnection(to: .unix(path: socketPath), using: .tcp)
        connection = conn
        conn.stateUpdateHandler = { [weak self] state in
            self?.handle(state: state, for: conn)
        }
        conn.start(queue: queue)
    }

    private func handle(state: NWConnection.State, for conn: NWConnection) {
        guard conn === connection else { return }
        switch state {
        case .ready:
            helloID = write(["op": "hello", "client": "app", "version": appVersion])
            write(["op": "subscribe", "topics": ["events", "snapshot"]])
            receive(on: conn)
        case .failed(let error):
            log.debug("connection failed: \(error.localizedDescription, privacy: .public)")
            dropAndScheduleReconnect()
        case .waiting(let error):
            // A missing socket file reports `waiting`; treat it as a failure.
            log.debug("connection waiting: \(error.localizedDescription, privacy: .public)")
            dropAndScheduleReconnect()
        case .cancelled:
            break
        default:
            break
        }
    }

    private func receive(on conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 256 * 1024) { [weak self] data, _, isComplete, error in
            guard let self, conn === self.connection else { return }
            if let data, !data.isEmpty {
                for line in self.framer.append(data) { self.dispatch(DaemonMessage.parse(line)) }
            }
            if isComplete || error != nil {
                self.dropAndScheduleReconnect()
            } else {
                self.receive(on: conn)
            }
        }
    }

    private func dispatch(_ message: DaemonMessage) {
        switch message {
        case .reply(let id, let ok, let error):
            if id == helloID {
                helloID = nil
                if ok {
                    connectedAt = Date()
                    setOnline(true)
                } else {
                    log.error("hello rejected: \(error ?? "?", privacy: .public)")
                    dropAndScheduleReconnect()
                }
            } else if !ok {
                log.info("request \(id) failed: \(error ?? "?", privacy: .public)")
            }
        case .event(let event):
            continuation.yield(.event(event))
        case .snapshot(let data):
            continuation.yield(.snapshot(data))
        case .command, .unknown:
            break
        }
    }

    /// Send one request; returns its id (nil when nothing was sent).
    @discardableResult
    private func write(_ message: [String: Any]) -> Int? {
        guard let conn = connection else { return nil }
        let id = nextID
        nextID += 1
        var framed = message
        framed["proto"] = 1
        framed["id"] = id
        guard var data = try? JSONSerialization.data(withJSONObject: framed) else { return nil }
        data.append(0x0A)
        conn.send(content: data, completion: .contentProcessed { [weak self] error in
            if let error { self?.log.debug("send failed: \(error.localizedDescription, privacy: .public)") }
        })
        return id
    }

    private func dropAndScheduleReconnect() {
        connection?.cancel()
        connection = nil
        if let connectedAt { backoff.connected(for: Date().timeIntervalSince(connectedAt)) }
        connectedAt = nil
        setOnline(false)
        guard !stopped else { return }
        reconnectItem?.cancel()
        let delay = backoff.nextDelay()
        let item = DispatchWorkItem { [weak self] in self?.connect() }
        reconnectItem = item
        queue.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func setOnline(_ value: Bool) {
        guard value != isOnline else { return }
        isOnline = value
        continuation.yield(.online(value))
    }
}
