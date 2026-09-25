import SwiftUI

/// Drawn stand-ins for what an offscreen capture can't include: the desktop, window title
/// bars, the Settings toolbar and the menu bar. The content inside them is the real UI.

struct Desktop<Content: View>: View {
    let appearance: Appearance
    @ViewBuilder let content: Content

    var body: some View {
        content
            .padding(36)
            .background(Self.color(appearance))
    }

    /// Flat on purpose: gradients cost several times the PNG size.
    static func color(_ appearance: Appearance) -> Color {
        appearance == .dark ? Color(red: 0.13, green: 0.14, blue: 0.21) : Color(red: 0.87, green: 0.89, blue: 0.95)
    }
}

/// A desktop widget's rounded card (the WidgetKit container background).
struct WidgetCard<Content: View>: View {
    let size: CGSize
    let appearance: Appearance
    @ViewBuilder let content: Content

    var body: some View {
        content
            .padding(15)
            .frame(width: size.width, height: size.height)
            .background(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(appearance == .dark ? Color(white: 0.13).opacity(0.92) : Color.white.opacity(0.88))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .strokeBorder(Color.primary.opacity(appearance == .dark ? 0.12 : 0.06), lineWidth: 0.5)
            )
            .shadow(color: .black.opacity(appearance == .dark ? 0.45 : 0.14), radius: 10, y: 4)
    }
}

enum CardSize {
    static let small = CGSize(width: 164, height: 164)
    static let medium = CGSize(width: 344, height: 164)
    static let large = CGSize(width: 344, height: 344)
}

struct TrafficLights: View {
    var body: some View {
        HStack(spacing: 8) {
            ForEach([Color(red: 1, green: 0.37, blue: 0.34), Color(red: 1, green: 0.74, blue: 0.18), Color(red: 0.16, green: 0.79, blue: 0.25)], id: \.self) { color in
                Circle().fill(color).frame(width: 12, height: 12)
            }
        }
    }
}

/// A window around a captured content image: title bar (plus an optional toolbar row).
struct WindowFrame<Toolbar: View>: View {
    let title: String
    let content: CGImage
    let appearance: Appearance
    @ViewBuilder let toolbar: Toolbar

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 6) {
                ZStack {
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.primary.opacity(0.85))
                    HStack {
                        TrafficLights()
                        Spacer()
                    }
                }
                toolbar
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)
            .padding(.bottom, 8)
            .frame(maxWidth: .infinity)
            .background(appearance == .dark ? Color(white: 0.20) : Color(white: 0.95))
            Divider()
            Image(captured: content)
                .background(appearance == .dark ? Color(white: 0.15) : Color(white: 0.925))
        }
        .fixedSize()
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.primary.opacity(appearance == .dark ? 0.25 : 0.12), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(0.35), radius: 22, y: 10)
    }
}

extension WindowFrame where Toolbar == EmptyView {
    init(title: String, content: CGImage, appearance: Appearance) {
        self.init(title: title, content: content, appearance: appearance) { EmptyView() }
    }
}

/// The Settings window's pane tabs (SwiftUI shows them as toolbar items).
struct SettingsToolbar: View {
    let selected: SettingsTab

    static let items: [(SettingsTab, String, String)] = [
        (.general, "General", "gearshape"),
        (.profiles, "Profiles", "person.2"),
        (.notifications, "Notifications", "bell.badge"),
        (.advanced, "Advanced", "gearshape.2"),
        (.about, "About", "info.circle"),
    ]

    var body: some View {
        HStack(spacing: 4) {
            ForEach(Self.items, id: \.0) { tab, label, symbol in
                VStack(spacing: 3) {
                    Image(systemName: symbol)
                        .font(.system(size: 17))
                        .frame(height: 20)
                    Text(label).font(.system(size: 11))
                }
                .foregroundStyle(tab == selected ? Color.accentColor : Color.primary.opacity(0.75))
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(
                    RoundedRectangle(cornerRadius: 7, style: .continuous)
                        .fill(tab == selected ? Color.primary.opacity(0.09) : .clear)
                )
            }
        }
    }
}

/// A slice of the menu bar with the CC Supervisor label and a few generic items.
struct MenuBarStrip: View {
    let label: CGImage
    let width: CGFloat
    let appearance: Appearance

    var body: some View {
        HStack(spacing: 16) {
            Spacer()
            Image(captured: label)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(RoundedRectangle(cornerRadius: 5).fill(Color.primary.opacity(0.12)))
            Image(systemName: "wifi")
            Image(systemName: "battery.75percent")
            Image(systemName: "switch.2")
            Text("Fri 14:05")
        }
        .font(.system(size: 13, weight: .medium))
        .foregroundStyle(.primary)
        .padding(.horizontal, 14)
        .frame(width: width, height: 30)
        .background(appearance == .dark ? Color.black.opacity(0.35) : Color.white.opacity(0.45))
    }
}

/// The menu bar dropdown panel around the captured menu content.
struct MenuPanel: View {
    let content: CGImage
    let appearance: Appearance

    var body: some View {
        Image(captured: content)
            .background(appearance == .dark ? Color(white: 0.17) : Color(white: 0.95))
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Color.primary.opacity(appearance == .dark ? 0.25 : 0.12), lineWidth: 0.5)
            )
            .shadow(color: .black.opacity(0.3), radius: 18, y: 8)
    }
}

/// A Notification Center banner with CC Supervisor's text (titles and bodies come from the
/// demo events, i.e. from the Python notification templates).
struct NotificationBanner: View {
    let icon: NSImage?
    let title: String
    let message: String
    let age: String
    let appearance: Appearance

    var body: some View {
        HStack(alignment: .center, spacing: 11) {
            if let icon {
                Image(nsImage: icon).resizable().frame(width: 36, height: 36)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                    Spacer(minLength: 8)
                    Text(age).font(.system(size: 11)).foregroundStyle(.secondary)
                }
                if !message.isEmpty {
                    Text(message).font(.system(size: 13)).foregroundStyle(.primary.opacity(0.85)).lineLimit(2)
                }
            }
        }
        .padding(.horizontal, 13)
        .padding(.vertical, 11)
        .frame(width: 400, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(appearance == .dark ? Color(white: 0.17).opacity(0.96) : Color.white.opacity(0.94))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(Color.primary.opacity(appearance == .dark ? 0.2 : 0.08), lineWidth: 0.5)
        )
        .shadow(color: .black.opacity(appearance == .dark ? 0.4 : 0.15), radius: 12, y: 5)
    }
}
