import Foundation

/// Why `widget/snapshot.json` couldn't be loaded (P12).
public enum SnapshotLoadError: Error, Equatable, Sendable {
    /// No file yet (daemon never ran, or not installed).
    case missing
    /// The file exists but can't be read (permissions, I/O).
    case unreadable(String)
    /// The bytes aren't a snapshot we can decode.
    case decode(String)
    /// The widget sandbox refused the read (EPERM): the temporary-exception
    /// entitlement is missing or the path is outside it (ADR-0012).
    case sandboxDenied
}

/// Reads the daemon-written display model. Read-only: the widget never writes
/// files, runs processes, or touches the network (ADR-0012).
public enum SnapshotLoader {
    public static func load(from url: URL = SnapshotLocation.snapshotFile()) -> Result<WidgetSnapshot, SnapshotLoadError> {
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return .failure(classify(error))
        }
        do {
            return .success(try SnapshotDecoding.decode(data))
        } catch {
            return .failure(.decode(String(describing: error)))
        }
    }

    /// Map a read error to a `SnapshotLoadError` via its POSIX cause.
    static func classify(_ error: Error) -> SnapshotLoadError {
        let ns = error as NSError
        let posix = posixCode(ns)
        switch posix {
        case ENOENT, ENOTDIR:
            return .missing
        case EPERM:
            return .sandboxDenied
        default:
            break
        }
        if ns.domain == NSCocoaErrorDomain,
           ns.code == CocoaError.fileReadNoSuchFile.rawValue || ns.code == CocoaError.fileNoSuchFile.rawValue {
            return .missing
        }
        return .unreadable(ns.localizedDescription)
    }

    private static func posixCode(_ error: NSError) -> Int32? {
        if error.domain == NSPOSIXErrorDomain { return Int32(error.code) }
        if let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError {
            return posixCode(underlying)
        }
        return nil
    }
}
