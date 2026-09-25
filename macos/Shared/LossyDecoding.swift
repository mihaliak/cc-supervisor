import Foundation

/// Lenient decoding shared by `config.json` (`AppConfig`) and `widget/snapshot.json`
/// (`WidgetSnapshot`): one bad value must not hide everything else.
extension KeyedDecodingContainer {
    /// Decode or `nil` (missing key, `null`, or a wrong type).
    func lossy<T: Decodable>(_ type: T.Type, _ key: Key) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }

    /// Decode an array, dropping elements that fail (e.g. a profile without an id).
    func lossyArray<T: Decodable>(_ type: T.Type, _ key: Key) -> [T]? {
        guard let items = try? decodeIfPresent([LossyElement<T>].self, forKey: key) else { return nil }
        return items.compactMap(\.value)
    }
}

private struct LossyElement<T: Decodable>: Decodable {
    let value: T?

    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}
