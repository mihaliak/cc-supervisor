import SwiftUI

/// SwiftUI bindings onto `config.json` fields. Reads fall back to the Python defaults;
/// writes go through `ConfigStore.set` (debounced save, then `ccs config validate`).
extension ConfigStore {
    func intBinding(_ field: FieldPath, fallback: Int = 0) -> Binding<Int> {
        Binding(
            get: { self.int(field) ?? fallback },
            set: { self.set(field, .int(Int64($0))) }
        )
    }

    func boolBinding(_ field: FieldPath, fallback: Bool = false) -> Binding<Bool> {
        Binding(
            get: { self.bool(field) ?? fallback },
            set: { self.set(field, .bool($0)) }
        )
    }

    /// With `emptyIsNull`, clearing the field writes `null` (e.g. `ccs_path`: use the default).
    func stringBinding(_ field: FieldPath, emptyIsNull: Bool = false) -> Binding<String> {
        Binding(
            get: { self.string(field) ?? "" },
            set: { self.set(field, emptyIsNull && $0.isEmpty ? .null : .string($0)) }
        )
    }

    /// `"HH:MM"` ↔ `Date` for `DatePicker(.hourAndMinute)`.
    func timeBinding(_ field: FieldPath, fallback: String) -> Binding<Date> {
        Binding(
            get: { TimeOfDay.date(from: self.string(field) ?? fallback) ?? TimeOfDay.date(from: fallback) ?? Date() },
            set: { self.set(field, .string(TimeOfDay.string(from: $0))) }
        )
    }

    /// Messages from the last validation for exactly this field.
    func issues(_ field: FieldPath) -> [String] {
        errors.messages(for: field)
    }
}
