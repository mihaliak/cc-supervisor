import SwiftUI

/// Shown while the daemon socket is offline: install / start actions (ADR-0011).
struct DaemonBannerView: View {
    @Environment(AppModel.self) private var model
    let state: DaemonBannerState

    var body: some View {
        if let content = Self.content(for: state) {
            VStack(alignment: .leading, spacing: 6) {
                Label(content.title, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(.orange)
                if let detail = content.detail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    switch state {
                    case .notInstalled:
                        Button("Install daemon") { model.installDaemon() }
                    case .stopped, .notResponding:
                        Button("Start daemon") { model.startDaemon() }
                    case .ccsMissing:
                        Button("Settings…") { model.requestSettings() }
                    default:
                        EmptyView()
                    }
                }
                .controlSize(.small)
                .disabled(model.isBusy("daemon"))
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    struct Content: Equatable {
        var title: String
        var detail: String?
    }

    static func content(for state: DaemonBannerState) -> Content? {
        switch state {
        case .hidden:
            nil
        case .checking:
            Content(title: "Connecting to the supervisor…", detail: nil)
        case .ccsMissing(let path):
            Content(title: "ccs not found", detail: "ccs not found at \(path). Set the path in Settings.")
        case .notInstalled:
            Content(title: "Supervisor daemon not installed", detail: "Usage stays frozen until the daemon runs.")
        case .stopped:
            Content(title: "Supervisor daemon stopped", detail: "Usage stays frozen until the daemon runs.")
        case .notResponding:
            Content(title: "Supervisor daemon not responding", detail: "Check `ccs daemon logs`.")
        case .error(let message):
            Content(title: "Supervisor offline", detail: message)
        }
    }
}
