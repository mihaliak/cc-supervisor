import Foundation

/// Result of one child process run.
struct ProcessResult: Sendable {
    let status: Int32
    let stdout: Data
    let stderr: Data
    let timedOut: Bool
}

/// Thread-safe byte buffer used by pipe readers.
private final class LockedData: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()

    func set(_ value: Data) {
        lock.lock(); defer { lock.unlock() }
        data = value
    }

    var value: Data {
        lock.lock(); defer { lock.unlock() }
        return data
    }
}

private final class LockedFlag: @unchecked Sendable {
    private var flag = false
    private let lock = NSLock()

    func set() {
        lock.lock(); defer { lock.unlock() }
        flag = true
    }

    var value: Bool {
        lock.lock(); defer { lock.unlock() }
        return flag
    }
}

/// Owns a `Process` so it can cross into `@Sendable` closures (all access is to
/// thread-safe members: `isRunning`, `terminate`, `processIdentifier`).
private final class ProcessBox: @unchecked Sendable {
    let process: Process
    init(_ process: Process) { self.process = process }
}

enum ProcessRunner {
    /// Run `executable` off the main actor, capturing stdout/stderr; terminate
    /// (SIGTERM, then SIGKILL after 2 s) when `timeout` elapses.
    static func run(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval
    ) async throws -> ProcessResult {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        let stdoutBuf = LockedData()
        let stderrBuf = LockedData()
        let timedOut = LockedFlag()
        let box = ProcessBox(process)
        let readers = DispatchGroup()

        return try await withCheckedThrowingContinuation { continuation in
            process.terminationHandler = { finished in
                let status = finished.terminationStatus
                DispatchQueue.global().async {
                    // A grandchild could keep the pipe open; don't wait forever for EOF.
                    _ = readers.wait(timeout: .now() + 2)
                    continuation.resume(returning: ProcessResult(
                        status: status,
                        stdout: stdoutBuf.value,
                        stderr: stderrBuf.value,
                        timedOut: timedOut.value
                    ))
                }
            }
            do {
                try process.run()
            } catch {
                process.terminationHandler = nil
                continuation.resume(throwing: CcsError.launch(error.localizedDescription))
                return
            }
            readers.enter()
            DispatchQueue.global().async {
                stdoutBuf.set(outPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }
            readers.enter()
            DispatchQueue.global().async {
                stderrBuf.set(errPipe.fileHandleForReading.readDataToEndOfFile())
                readers.leave()
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard box.process.isRunning else { return }
                timedOut.set()
                box.process.terminate()
                DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                    if box.process.isRunning { kill(box.process.processIdentifier, SIGKILL) }
                }
            }
        }
    }
}
