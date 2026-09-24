import AppIntents

/// A CC Supervisor profile as offered by right-click → Edit Widget.
struct ProfileEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Profile"
    static let defaultQuery = ProfileQuery()

    let id: String
    let name: String
    let emoji: String

    init(id: String, name: String, emoji: String) {
        self.id = id
        self.name = name
        self.emoji = emoji
    }

    init(_ choice: WidgetProfileChoice) {
        self.init(id: choice.id, name: choice.name, emoji: choice.emoji)
    }

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(emoji.isEmpty ? name : "\(emoji) \(name)")")
    }
}
