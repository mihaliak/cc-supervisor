import Foundation
import Observation
import os

/// What `ccs config validate --json` said after a save, or why it could not run.
enum ValidationOutcome: Sendable, Equatable {
    case report(ValidationReport)
    case unavailable(String)
    /// The run was cancelled or superseded; nothing to report (keep the previous state).
    case cancelled
}

/// One edit not yet on disk. `base` is the value on disk when the edit started (nil = the key
/// was absent), so a concurrent writer's change to the same field can be told apart from ours.
struct PendingChange: Sendable, Equatable {
    var field: FieldPath
    var base: JSONValue?
    var value: JSONValue?
}

/// Another writer (CLI, daemon, a second window) changed a field we also edited.
struct ConfigConflict: Sendable, Equatable {
    var fields: [FieldPath]
    var diskRevision: Int
}

enum ConflictResolution: Sendable {
    case keepMine
    case takeTheirs
}

/// Another writer held `.config.lock` for longer than `ConfigStore.lockTimeout`.
struct ConfigLockTimeout: LocalizedError {
    var errorDescription: String? { "another ccs process holds .config.lock. Your changes are kept and saved with the next edit." }
}

enum SaveResult: Sendable, Equatable {
    case saved(revision: Int)
    case nothingToSave
    case conflict([FieldPath])
    case failed(String)
}

/// Reads and writes `config.json` for the Settings window (ADR-0004).
///
/// - Edits go into the raw JSON tree, so unknown keys are never dropped.
/// - Saves are debounced, revision-checked and atomic (temp file + replace), under the same
///   `.config.lock` Python takes. The file is written in exactly the format Python writes
///   (`JSONValue.canonicalText()`).
/// - After a write, `ccs config validate --json` runs and its issues are shown inline. Swift
///   never validates by itself (ADR-0001); invalid values stay written and the daemon keeps
///   using its last good config until they are fixed.
@MainActor
@Observable
final class ConfigStore {
    let fileURL: URL

    private(set) var raw: JSONValue = .object([:])
    private(set) var model = ConfigModel()
    /// `revision` of the document we last read or wrote (nil: absent).
    private(set) var loadedRevision: Int?
    private(set) var fileExists = false
    /// Set when `config.json` exists but is not a JSON object; editing is disabled so the
    /// user's file is never overwritten with a guess.
    private(set) var loadError: String?
    private(set) var saveError: String?
    private(set) var errors = ValidationErrors()
    /// True when the last validation found issues (the daemon keeps its previous config).
    private(set) var isInvalid = false
    private(set) var validationProblem: String?
    private(set) var conflict: ConfigConflict?
    private(set) var pending: [PendingChange] = []
    /// `ccs config defaults --json`: `{"config": {…}, "profile": {…}}`.
    private(set) var defaults: JSONValue?
    private(set) var isSaving = false

    @ObservationIgnored var saveDelay: Duration = .milliseconds(500)
    /// How long a save waits for another writer (`ccs`) to release `.config.lock`.
    @ObservationIgnored var lockTimeout: TimeInterval = 2
    @ObservationIgnored var validator: (@MainActor () async -> ValidationOutcome)?
    @ObservationIgnored var onSaved: (@MainActor () -> Void)?
    @ObservationIgnored private var saveTask: Task<Void, Never>?
    /// Bumped per validation run; only the newest run's result is applied.
    @ObservationIgnored private var validationGeneration = 0
    @ObservationIgnored private var diskData: Data?
    @ObservationIgnored private let log = Logger(subsystem: "local.ccsupervisor.app", category: "config")

    init(fileURL: URL = LaunchAgentEnvironment.configFile) {
        self.fileURL = fileURL
        load()
    }

    // MARK: - Loading

    /// Re-read the file. Pending edits (and a conflict about them) are dropped; call only when
    /// there are none, or use `reloadKeepingEdits()`.
    func load() {
        conflict = nil
        guard let data = try? Data(contentsOf: fileURL) else {
            fileExists = FileManager.default.fileExists(atPath: fileURL.path)
            loadError = fileExists ? "config.json can't be read" : nil
            raw = .object([:])
            diskData = nil
            loadedRevision = nil
            model = ConfigModel()
            pending = []
            return
        }
        fileExists = true
        diskData = data
        pending = []
        do {
            let parsed = try JSONValue.parse(data)
            guard parsed.objectValue != nil else { throw JSONParseError.unexpectedEnd }
            raw = parsed
            loadError = nil
        } catch {
            raw = .object([:])
            loadError = "config.json is not a valid JSON object (\(error.localizedDescription)). Fix it by hand or run `ccs config validate`."
        }
        loadedRevision = raw["revision"]?.intValue
        model = ConfigModel.from(raw)
    }

    /// Pick up changes other writers made, unless we are in the middle of editing.
    /// Returns true when the view changed.
    @discardableResult
    func reloadIfChanged() -> Bool {
        guard pending.isEmpty, conflict == nil, !isSaving else { return false }
        let data = try? Data(contentsOf: fileURL)
        guard data != diskData || (data == nil && fileExists) else { return false }
        load()
        return true
    }

    /// Pick up a change we asked `ccs` to make (`profile add|remove`) without dropping edits
    /// made meanwhile: edits of a profile that is gone are dropped, the rest are merged onto
    /// the new file and saved like any save (a same-field change raises the conflict alert).
    /// Validates the result.
    func reloadKeepingEdits() async {
        if !pending.isEmpty, let data = try? Data(contentsOf: fileURL), let disk = try? JSONValue.parse(data) {
            pending.removeAll { $0.field.resolved(in: disk) == nil }
        }
        guard !pending.isEmpty else {
            load()
            await validate()
            return
        }
        // `.saved` validated already; a conflict or failure keeps the edits (and says so).
        if await saveNow() == .nothingToSave { await validate() }
    }

    func setDefaults(_ value: JSONValue?) {
        defaults = value
    }

    /// Editing is possible only on a readable, existing file without an open conflict.
    var canEdit: Bool {
        fileExists && loadError == nil && conflict == nil
    }

    var profiles: [ConfigModel.Profile] { model.profiles ?? [] }

    var defaultProfileID: String? { model.defaultProfile }

    // MARK: - Reading values

    /// The value in the file, else the Python default (`ccs config defaults`). JSON `null`
    /// counts as absent.
    func value(_ field: FieldPath) -> JSONValue? {
        if let path = field.resolved(in: raw), let v = raw.value(at: path), v != .null { return v }
        return defaultValue(field)
    }

    func defaultValue(_ field: FieldPath) -> JSONValue? {
        let section = field.profileID == nil ? "config" : "profile"
        guard let v = defaults?[section]?.value(at: field.components), v != .null else { return nil }
        return v
    }

    func int(_ field: FieldPath) -> Int? { value(field)?.intValue }
    func bool(_ field: FieldPath) -> Bool? { value(field)?.boolValue }
    func string(_ field: FieldPath) -> String? { value(field)?.stringValue }

    // MARK: - Editing

    /// Change (or with `nil`, remove) a field and schedule a debounced save.
    func set(_ field: FieldPath, _ newValue: JSONValue?) {
        guard canEdit, let path = field.resolved(in: raw) else { return }
        let current = raw.value(at: path)
        guard current != newValue else { return }
        if let i = pending.firstIndex(where: { $0.field == field }) {
            if pending[i].base == newValue {
                pending.remove(at: i)
            } else {
                pending[i].value = newValue
            }
        } else {
            pending.append(PendingChange(field: field, base: current, value: newValue))
        }
        raw.setValue(newValue, at: path)
        model = ConfigModel.from(raw)
        saveError = nil
        scheduleSave()
    }

    private func scheduleSave() {
        saveTask?.cancel()
        let delay = saveDelay
        saveTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            await self?.runScheduledSave()
        }
    }

    /// The debounced autosave. Runs inside `saveTask`, so unlike `saveNow()` it must not
    /// cancel that task: doing so cancelled its own `ccs config validate` run, which then
    /// surfaced as "Couldn't validate the config … CancellationError". Detaching it from
    /// `saveTask` also means a newer edit schedules a new save without killing this one.
    private func runScheduledSave() async {
        saveTask = nil
        let result = write(resolution: nil)
        await afterWrite(result)
    }

    /// Write pending edits now (explicit actions, window close) and validate.
    @discardableResult
    func saveNow() async -> SaveResult {
        saveTask?.cancel()
        saveTask = nil
        let result = write(resolution: nil)
        await afterWrite(result)
        return result
    }

    /// Quitting: write pending edits now, synchronously (no validation, the app is going away;
    /// the daemon checks the file itself).
    @discardableResult
    func saveBeforeQuit() -> SaveResult {
        saveTask?.cancel()
        saveTask = nil
        return write(resolution: nil)
    }

    /// Answer the conflict alert.
    @discardableResult
    func resolveConflict(_ resolution: ConflictResolution) async -> SaveResult {
        guard conflict != nil else { return .nothingToSave }
        let result = write(resolution: resolution)
        await afterWrite(result)
        return result
    }

    private func afterWrite(_ result: SaveResult) async {
        guard case .saved = result else { return }
        await validate()
        onSaved?()
    }

    /// Run `ccs config validate --json` and map its issues onto fields.
    func validate() async {
        guard let validator else {
            validationProblem = "Validation unavailable"
            return
        }
        validationGeneration += 1
        let generation = validationGeneration
        let validated = raw
        let outcome = await validator()
        // A newer run started meanwhile: its result wins, drop this one.
        guard generation == validationGeneration else { return }
        switch outcome {
        case .report(let report):
            errors = ValidationErrors(report: report, validated: validated)
            isInvalid = !report.ok
            validationProblem = nil
        case .unavailable(let why):
            validationProblem = why
        case .cancelled:
            break
        }
    }

    /// The write itself (synchronous file IO on the main actor; the file is tiny).
    private func write(resolution: ConflictResolution?) -> SaveResult {
        guard !pending.isEmpty else {
            conflict = nil
            return .nothingToSave
        }
        isSaving = true
        defer { isSaving = false }
        do {
            return try Self.withConfigLock(Self.lockURL(for: fileURL), timeout: lockTimeout) {
                writeLocked(resolution: resolution)
            }
        } catch {
            return fail("Couldn't save config.json: \(error.localizedDescription)")
        }
    }

    /// Read → merge → replace, holding `.config.lock`.
    private func writeLocked(resolution: ConflictResolution?) -> SaveResult {
        let currentData: Data
        do {
            currentData = try Data(contentsOf: fileURL)
        } catch {
            return fail("config.json disappeared while editing (\(error.localizedDescription))")
        }
        let disk: JSONValue
        do {
            disk = try JSONValue.parse(currentData)
            guard disk.objectValue != nil else { throw JSONParseError.unexpectedEnd }
        } catch {
            return fail("config.json on disk is not valid JSON; not overwriting it")
        }
        let diskRevision = disk["revision"]?.intValue ?? 0

        var target: JSONValue
        if currentData == diskData {
            target = raw
        } else {
            // Another writer got there first: re-apply our edits onto the fresh document.
            var fresh = disk
            var conflicts: [FieldPath] = []
            for change in pending {
                guard let path = change.field.resolved(in: fresh) else {
                    conflicts.append(change.field)
                    continue
                }
                let onDisk = fresh.value(at: path)
                if onDisk != change.base && onDisk != change.value {
                    switch resolution {
                    case .keepMine: break
                    case .takeTheirs: continue
                    case nil:
                        conflicts.append(change.field)
                        continue
                    }
                }
                fresh.setValue(change.value, at: path)
            }
            let unresolved = resolution == .keepMine
                ? conflicts.filter { $0.resolved(in: fresh) == nil }
                : conflicts
            if resolution == nil, !unresolved.isEmpty {
                conflict = ConfigConflict(fields: unresolved, diskRevision: diskRevision)
                return .conflict(unresolved)
            }
            if fresh == disk {
                // Nothing of ours is left to write: adopt the disk version.
                raw = disk
                diskData = currentData
                loadedRevision = disk["revision"]?.intValue
                model = ConfigModel.from(raw)
                pending = []
                conflict = nil
                return .nothingToSave
            }
            target = fresh
        }

        let newRevision = diskRevision + 1
        target.setValue(.int(Int64(newRevision)), at: [.key("revision")])
        let data = target.canonicalData()
        do {
            try Self.atomicWrite(data, to: fileURL)
        } catch {
            return fail("Couldn't save config.json: \(error.localizedDescription)")
        }
        raw = target
        diskData = data
        loadedRevision = newRevision
        model = ConfigModel.from(raw)
        pending = []
        conflict = nil
        saveError = nil
        log.info("saved config.json revision \(newRevision)")
        return .saved(revision: newRevision)
    }

    private func fail(_ message: String) -> SaveResult {
        saveError = message
        log.error("\(message, privacy: .public)")
        return .failed(message)
    }

    /// `<config dir>/.config.lock`, the lock `ccs.config.store` holds while it reads, checks
    /// the revision and replaces `config.json`.
    nonisolated static func lockURL(for configFile: URL) -> URL {
        configFile.deletingLastPathComponent().appendingPathComponent(".config.lock")
    }

    /// Run `body` holding `flock(LOCK_EX)` on `lockFile` (created 0600 if missing, like
    /// Python's `FileLock`). Waits up to `timeout` for another holder, polling, so a stuck
    /// `ccs` can't hang the UI.
    nonisolated static func withConfigLock<T>(_ lockFile: URL, timeout: TimeInterval, _ body: () throws -> T) throws -> T {
        let fd = open(lockFile.path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        let deadline = Date().addingTimeInterval(timeout)
        while flock(fd, LOCK_EX | LOCK_NB) != 0 {
            let error = errno
            guard error == EWOULDBLOCK || error == EINTR else {
                throw POSIXError(POSIXErrorCode(rawValue: error) ?? .EIO)
            }
            guard Date() < deadline else { throw ConfigLockTimeout() }
            usleep(10_000)
        }
        defer { flock(fd, LOCK_UN) }
        return try body()
    }

    /// Temp file in the same directory, fsync, then replace; the original's permissions are kept.
    static func atomicWrite(_ data: Data, to url: URL) throws {
        let fm = FileManager.default
        let dir = url.deletingLastPathComponent()
        let tmp = dir.appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        let mode = (try? fm.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber)?.intValue ?? 0o600
        guard fm.createFile(atPath: tmp.path, contents: data, attributes: [.posixPermissions: mode]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        do {
            let handle = try FileHandle(forWritingTo: tmp)
            try handle.synchronize()
            try handle.close()
            if fm.fileExists(atPath: url.path) {
                _ = try fm.replaceItemAt(url, withItemAt: tmp)
            } else {
                try fm.moveItem(at: tmp, to: url)
            }
        } catch {
            try? fm.removeItem(at: tmp)
            throw error
        }
    }
}
