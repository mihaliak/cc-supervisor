import Foundation
import Observation
import os

/// Watches `widget/snapshot.json` (written atomically by the daemon, so the
/// directory is watched — a replace swaps the inode) with a 30 s safety re-read.
/// Socket `snapshot` pushes update it immediately too.
@MainActor
@Observable
final class SnapshotStore {
    private(set) var snapshot: WidgetSnapshot?
    private(set) var lastError: String?
    private(set) var loadedAt: Date?

    /// Called after every successful load/apply with the new snapshot.
    @ObservationIgnored var onChange: ((WidgetSnapshot) -> Void)?

    @ObservationIgnored let fileURL: URL
    @ObservationIgnored private var source: DispatchSourceFileSystemObject?
    @ObservationIgnored private var timer: Timer?
    @ObservationIgnored private let log = Logger(subsystem: "local.ccsupervisor.app", category: "snapshot")

    init(fileURL: URL = LaunchAgentEnvironment.sourceSnapshotFile) {
        self.fileURL = fileURL
    }

    var generatedAt: Date? { snapshot?.generatedAt }

    func start() {
        reload()
        installWatcher()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                if self.source == nil { self.installWatcher() }
                self.reload()
            }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        source?.cancel()
        source = nil
    }

    /// Re-read the file. A missing file means "no data yet" (never created here).
    func reload() {
        guard let data = try? Data(contentsOf: fileURL) else {
            if snapshot == nil { lastError = "no snapshot yet" }
            return
        }
        apply(data: data)
    }

    /// Decode bytes from the file or a socket push.
    func apply(data: Data) {
        do {
            let decoded = try SnapshotDecoding.decode(data)
            lastError = nil
            loadedAt = Date()
            guard decoded != snapshot else { return }
            snapshot = decoded
            log.notice("snapshot updated: \(decoded.profiles.count) profiles")
            mirrorIfNeeded(data)
            onChange?(decoded)
        } catch {
            lastError = "unreadable snapshot: \(error.localizedDescription)"
            log.error("snapshot decode failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func installWatcher() {
        source?.cancel()
        source = nil
        let dir = fileURL.deletingLastPathComponent()
        let fd = open(dir.path, O_EVTONLY)
        guard fd >= 0 else { return }  // directory not created yet; the timer retries
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete, .extend, .attrib],
            queue: .main
        )
        src.setEventHandler { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                if let event = self.source?.data, event.contains(.delete) || event.contains(.rename) {
                    // The directory itself went away; re-arm on the next timer tick.
                    self.source?.cancel()
                    self.source = nil
                }
                self.reload()
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        source = src
    }

    /// ADR-0012 fallback A: mirror into the App Group container for the widget.
    private func mirrorIfNeeded(_ data: Data) {
        #if CCS_WIDGET_APPGROUP
        let target = SnapshotLocation.snapshotFile(environment: LaunchAgentEnvironment.resolved)
        guard target != fileURL else { return }
        try? FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: target, options: .atomic)
        #endif
    }
}
