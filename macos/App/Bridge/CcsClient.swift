import Foundation

/// Runs `ccs <args> --json` and decodes the reply (ADR-0011: every action goes
/// through the Python CLI; ADR-0001: no logic here).
actor CcsClient {
    let executablePath: String

    init(executablePath: String) {
        self.executablePath = executablePath
    }

    /// Environment for the child: GUI apps get a minimal PATH, so prepend the
    /// usual tool locations (`ccs` itself resolves `claude` via config/daemon).
    static func childEnvironment(
        base: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = StateLocation.realHome()
    ) -> [String: String] {
        var env = base
        let prefix = [
            home.appendingPathComponent(".local/bin").path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
        let existing = (base["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
            .split(separator: ":").map(String.init)
        env["PATH"] = (prefix + existing.filter { !prefix.contains($0) }).joined(separator: ":")
        return env
    }

    /// Run and decode. With `decodeOnFailure`, a non-zero exit that still printed
    /// JSON decodes into `T` (e.g. `doctor` exits 1 when a check fails).
    func run<T: Decodable & Sendable>(
        _ args: [String],
        as type: T.Type = T.self,
        timeout: Duration = .seconds(30),
        decodeOnFailure: Bool = false
    ) async throws -> T {
        let url = URL(fileURLWithPath: executablePath)
        guard FileManager.default.isExecutableFile(atPath: url.path) else {
            throw CcsError.notFound(path: executablePath)
        }
        let seconds = Int(timeout.components.seconds)
        let result = try await ProcessRunner.run(
            executable: url,
            arguments: args + ["--json"],
            environment: Self.childEnvironment(),
            timeout: TimeInterval(seconds)
        )
        if result.timedOut { throw CcsError.timeout(seconds: seconds) }
        if result.status != 0 && !decodeOnFailure {
            throw CcsError.failed(exitCode: result.status, message: Self.failureMessage(result))
        }
        do {
            return try Self.decoder.decode(T.self, from: result.stdout)
        } catch {
            if result.status != 0 {
                throw CcsError.failed(exitCode: result.status, message: Self.failureMessage(result))
            }
            throw CcsError.decoding(String(describing: error))
        }
    }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// `error` from a JSON reply, else the last stderr line, else the exit code.
    static func failureMessage(_ result: ProcessResult) -> String {
        if let reply = try? decoder.decode(CcsReply.self, from: result.stdout),
           let error = reply.error, !error.isEmpty {
            return error
        }
        let tail = String(decoding: result.stderr, as: UTF8.self)
            .split(separator: "\n").last.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
        if !tail.isEmpty { return tail.hasPrefix("ccs: ") ? String(tail.dropFirst(5)) : tail }
        return "ccs exited with code \(result.status)"
    }

    // MARK: - Typed helpers (contracts: ADR-0017, P04/P06/P08/P09/P13)

    func status() async throws -> StatusResult {
        try await run(["status"])
    }

    func usageRefresh(profile: String? = nil) async throws -> CcsReply {
        try await run(["usage", "--refresh"] + (profile.map { ["--profile", $0] } ?? []), timeout: .seconds(60))
    }

    func doctor() async throws -> DoctorResult {
        try await run(["doctor"], timeout: .seconds(60), decodeOnFailure: true)
    }

    func authLogin(profile: String) async throws -> AuthLoginResult {
        try await run(["auth", "login", "--profile", profile], timeout: .seconds(600), decodeOnFailure: true)
    }

    func authStatus(profile: String) async throws -> AuthStatusResult {
        try await run(["auth", "status", "--profile", profile])
    }

    func warmup(profile: String, trigger: String = "manual") async throws -> WarmupResult {
        try await run(["warmup", "--profile", profile, "--trigger", trigger])
    }

    func warmupAll(trigger: String) async throws -> WarmupResult {
        try await run(["warmup", "--all", "--trigger", trigger])
    }

    func pause(profile: String) async throws -> CcsReply {
        try await run(["pause", "--profile", profile])
    }

    func resume(profile: String) async throws -> CcsReply {
        try await run(["resume", "--profile", profile])
    }

    func daemonStatus() async throws -> DaemonStatusResult {
        try await run(["daemon", "status"], timeout: .seconds(15))
    }

    func daemonInstall() async throws -> CcsReply {
        try await run(["daemon", "install"], timeout: .seconds(60))
    }

    func daemonStart() async throws -> CcsReply {
        try await run(["daemon", "start"], timeout: .seconds(60))
    }
}
