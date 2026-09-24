import Foundation
import XCTest

/// Loads files from `schema/fixtures/` (copied into the test bundle as a folder).
enum Fixtures {
    private final class Token {}

    static var root: URL {
        guard let url = Bundle(for: Token.self).url(forResource: "fixtures", withExtension: nil) else {
            fatalError("schema/fixtures missing from the test bundle")
        }
        return url
    }

    static func data(_ relativePath: String) throws -> Data {
        try Data(contentsOf: root.appendingPathComponent(relativePath))
    }

    static func files(in directory: String) throws -> [URL] {
        try FileManager.default
            .contentsOfDirectory(at: root.appendingPathComponent(directory), includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    static func date(_ iso: String) -> Date {
        guard let date = ISO8601DateFormatter().date(from: iso) else { fatalError("bad date \(iso)") }
        return date
    }
}
