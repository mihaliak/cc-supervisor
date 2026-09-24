import AppIntents

/// Lists the profiles in `widget/snapshot.json` (the daemon writes every configured
/// profile there). The first one is the default for a newly added widget.
struct ProfileQuery: EntityQuery {
    func entities(for identifiers: [ProfileEntity.ID]) async throws -> [ProfileEntity] {
        WidgetProfileCatalog.choices(for: identifiers, in: Self.snapshot()).map(ProfileEntity.init)
    }

    func suggestedEntities() async throws -> [ProfileEntity] {
        WidgetProfileCatalog.choices(Self.snapshot()).map(ProfileEntity.init)
    }

    func defaultResult() async -> ProfileEntity? {
        WidgetProfileCatalog.defaultChoice(Self.snapshot()).map(ProfileEntity.init)
    }

    private static func snapshot() -> WidgetSnapshot? {
        try? SnapshotLoader.load().get()
    }
}
