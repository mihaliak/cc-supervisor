import Foundation

/// Result of one child process run.
struct ProcessResult: Sendable {
    let status: Int32
    let stdout: Data
    let stderr: Data
    let timedOut: Bool
}

/// Reads one pipe as data arrives, on a dispatch source: no thread waits for EOF, and
/// everything the child wrote is kept even if a grandchild holds the pipe open after the
/// child exits. Reading ends at EOF or `stop()`; then the read end is closed and `group`
/// (entered in `init`) is left.
private final class PipeReader: @unchecked Sendable {
    private static let queue = DispatchQueue(label: "local.ccsupervisor.process-pipes")

    private let fd: Int32
    private let source: DispatchSourceRead
    private let lock = NSLock()
    private var buffer = Data()

    init(_ handle: FileHandle, group: DispatchGroup) {
        fd = handle.fileDescriptor
        // Non-blocking, so a read can never stall the queue (and `stop()` behind it).
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
        source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: Self.queue)
        group.enter()
        // Handlers hold `self` until the source is cancelled (EOF or `stop()`), which
        // releases them.
        source.setEventHandler { self.readChunk() }
        source.setCancelHandler {
            try? handle.close()
            group.leave()
        }
        source.resume()
    }

    /// Stop reading and close the read end (idempotent). A writer still holding the pipe
    /// gets EPIPE from now on.
    func stop() {
        source.cancel()
    }

    var data: Data {
        lock.lock(); defer { lock.unlock() }
        return buffer
    }

    /// One read per event: the source fires again while more is buffered, and a writer
    /// that never stops can't keep the handler (and a pending `stop()`) from finishing.
    private func readChunk() {
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        var count: Int
        var error: Int32
        repeat {
            count = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            error = count < 0 ? errno : 0
        } while error == EINTR
        if count > 0 {
            lock.lock(); defer { lock.unlock() }
            buffer.append(contentsOf: chunk[0..<count])
        } else if count == 0 || error != EAGAIN {
            source.cancel()  // EOF, or a read error
        }
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
    /// How long to keep reading after the child exits: its output is already in the pipes,
    /// only a grandchild that inherited them can still be writing.
    static let drainTimeout: TimeInterval = 2

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

        let timedOut = LockedFlag()
        let cancelled = LockedFlag()
        let box = ProcessBox(process)
        // Readers start before the child can exit, so a fast child's output is never missed.
        let readers = DispatchGroup()
        let stdout = PipeReader(outPipe.fileHandleForReading, group: readers)
        let stderr = PipeReader(errPipe.fileHandleForReading, group: readers)

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                process.terminationHandler = { finished in
                    let status = finished.terminationStatus
                    // A grandchild could keep the pipes open; stop reading after the drain
                    // time and return what the child wrote (no thread waits on it).
                    DispatchQueue.global().asyncAfter(deadline: .now() + drainTimeout) {
                        stdout.stop()
                        stderr.stop()
                    }
                    readers.notify(queue: .global()) {
                        continuation.resume(returning: ProcessResult(
                            status: status,
                            stdout: stdout.data,
                            stderr: stderr.data,
                            timedOut: timedOut.value
                        ))
                    }
                }
                do {
                    try process.run()
                } catch {
                    process.terminationHandler = nil
                    stdout.stop()
                    stderr.stop()
                    continuation.resume(throwing: CcsError.launch(error.localizedDescription))
                    return
                }
                if cancelled.value { terminate(box, grace: grace) }
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
