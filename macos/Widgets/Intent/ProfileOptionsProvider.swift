import AppIntents

/// Profiles offered by right-click → Edit Widget, read from `widget/snapshot.json`
/// (the daemon writes every configured profile there).
///
/// The widget stores the chosen profile as a plain `String` id, not an `AppEntity`:
/// `linkd` won't serve App Intents metadata to ad-hoc signed builds, so entity
/// parameters always decoded to nil (ADR-0019).
struct ProfileOptionsProvider: DynamicOptionsProvider {
    func results() async throws -> ItemCollection<String> {
        let items = WidgetProfileCatalog.choices(Self.snapshot()).map {
            IntentItem($0.id, title: "\($0.title)")
        }
        return ItemCollection(sections: [ItemSection(items: items)])
    }

    /// The first profile, used when a widget is added.
    func defaultResult() async -> String? {
        WidgetProfileCatalog.defaultChoice(Self.snapshot())?.id
    }

    private static func snapshot() -> WidgetSnapshot? {
        try? SnapshotLoader.load().get()
    }
}
