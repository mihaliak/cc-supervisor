import AppIntents

/// Edit Widget → Small widget style: progress bar (default) or gauge. A plain `String`
/// parameter like the profile (ADR-0019).
struct SmallStyleOptionsProvider: DynamicOptionsProvider {
    func results() async throws -> ItemCollection<String> {
        let items = SmallWidgetStyle.allCases.map { IntentItem($0.rawValue, title: "\($0.title)") }
        return ItemCollection(sections: [ItemSection(items: items)])
    }

    func defaultResult() async -> String? {
        SmallWidgetStyle.bar.rawValue
    }
}
