import SwiftUI

/// Statusline on/off, preview (`ccs statusline preview`), apply and revert (P07).
/// Apply/revert touch the profile's `settings.json`, so they only run on a click.
struct StatuslineSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    var body: some View {
        let store = settings.store
        let preview = settings.preview[profileID]
        let busy = settings.isBusy("statusline:\(profileID)")
        Section("Statusline") {
            Toggle("Show the ccs statusline", isOn: store.boolBinding(.profile(profileID, "statusline.enabled"), fallback: true))
            if let samples = preview?.samples, !samples.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(samples) { sample in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(sample.state.replacingOccurrences(of: "_", with: " "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(width: 118, alignment: .trailing)
                            Text(SegmentsPreview.attributed(sample.segments ?? []))
                                .font(.system(.callout, design: .monospaced))
                                .lineLimit(1)
                                .truncationMode(.tail)
                                .textSelection(.enabled)
                        }
                    }
                }
                .padding(8)
                .background(.background.secondary, in: RoundedRectangle(cornerRadius: 6))
            }
            if let status = preview?.status {
                LabeledContent("Applied to settings.json", value: status.applied == true ? "Yes" : "No")
                if let path = status.scriptPath {
                    LabeledContent("Script") {
                        Text(path).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                }
                LabeledContent("Script up to date", value: status.scriptCurrent == true ? "Yes" : "No (regenerated on next ccs launch or apply)")
            }
            HStack {
                Button("Apply to settings.json") { settings.applyStatusline(profileID) }
                    .disabled(busy)
                Button("Revert") { settings.revertStatusline(profileID) }
                    .disabled(busy || preview?.status?.applied != true)
                if busy { ProgressView().controlSize(.small) }
                Spacer()
                Button {
                    settings.refreshPreview(profileID)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .help("Refresh the preview")
            }
            Text("`ccs --\(store.string(.profile(profileID, "flag")) ?? profileID)` always shows the statusline. Apply also makes plain `claude` in this config dir show it (a backup of settings.json is kept).")
                .font(.caption)
                .foregroundStyle(.secondary)
            MessageLine(message: settings.message("statusline:\(profileID)"))
        }
    }
}
