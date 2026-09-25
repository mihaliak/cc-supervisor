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

    /// Set the flag; true only for the call that set it.
    func setOnce() -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard !flag else { return false }
        flag = true
        return true
    }

    var value: Bool {
        lock.lock(); defer { lock.unlock() }
        return flag
    }
}

/// Owns a `Process` so it can cross into `@Sendable` closures (all access is to
/// thread-safe members: `isRunning`, `interrupt`, `terminate`, `processIdentifier`).
private final class ProcessBox: @unchecked Sendable {
    let process: Process
    /// Set by the first stop request (timeout or cancel), so the signals are sent once.
    let stopping = LockedFlag()
    init(_ process: Process) { self.process = process }
}

enum ProcessRunner {
    /// Run `executable` off the main actor, capturing stdout/stderr; stop it (see
    /// `terminate`) when `timeout` elapses or the calling task is cancelled (e.g. the user
    /// cancels a long sign-in). `grace` is the wait between the escalating signals.
    static func run(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval,
        grace: TimeInterval = 2
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
        let cancelled = LockedFlag()
        let box = ProcessBox(process)
        let readers = DispatchGroup()

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
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
                // Enter the group before the child can exit: a fast child's termination
                // handler must not find an empty group and return before its output is read.
                readers.enter()
                readers.enter()
                do {
                    try process.run()
                } catch {
                    process.terminationHandler = nil
                    readers.leave()
                    readers.leave()
                    continuation.resume(throwing: CcsError.launch(error.localizedDescription))
                    return
                }
                if cancelled.value { terminate(box, grace: grace) }
                DispatchQueue.global().async {
                    stdoutBuf.set(outPipe.fileHandleForReading.readDataToEndOfFile())
                    readers.leave()
                }
                DispatchQueue.global().async {
                    stderrBuf.set(errPipe.fileHandleForReading.readDataToEndOfFile())
                    readers.leave()
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                    guard box.process.isRunning else { return }
                    timedOut.set()
                    terminate(box, grace: grace)
                }
            }
        } onCancel: {
            cancelled.set()
            terminate(box, grace: grace)
        }
    }

    /// SIGINT first: `ccs` treats it as Ctrl-C and stops its own child's process group (e.g.
    /// `claude auth login`), which SIGTERM/SIGKILL would orphan. Then SIGTERM after `grace`,
    /// and SIGKILL after another `grace`, while it is still running.
    private static func terminate(_ box: ProcessBox, grace: TimeInterval) {
        guard box.process.isRunning, box.stopping.setOnce() else { return }
        box.process.interrupt()
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
            guard box.process.isRunning else { return }
            box.process.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
                if box.process.isRunning { kill(box.process.processIdentifier, SIGKILL) }
            }
        }
    }
}
