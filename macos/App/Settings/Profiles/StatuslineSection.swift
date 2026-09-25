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
        let flag = store.string(.profile(profileID, "flag")) ?? profileID

        Section {
            Toggle("Show the statusline in `ccs --\(flag)`", isOn: store.boolBinding(.profile(profileID, "statusline.enabled"), fallback: true))
            if let samples = preview?.samples, !samples.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(samples) { sample in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(sample.state.replacingOccurrences(of: "_", with: " "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(width: 96, alignment: .trailing)
                            Text(SegmentsPreview.attributed(sample.segments ?? []))
                                .font(.system(size: 10, design: .monospaced))
                                .lineLimit(1)
                                .truncationMode(.tail)
                                .textSelection(.enabled)
                        }
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.background.secondary, in: RoundedRectangle(cornerRadius: 6))
            }
        } header: {
            HStack {
                Text("Statusline")
                Spacer()
                Button {
                    settings.refreshPreview(profileID)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Refresh the preview")
            }
        }

        Section {
            LabeledContent("Applied", value: preview?.status?.applied == true ? "Yes" : "No")
            if let status = preview?.status {
                LabeledContent("Script", value: status.scriptCurrent == true ? "Up to date" : "Regenerated on next launch or apply")
            }
            HStack {
                if busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Revert") { settings.revertStatusline(profileID) }
                    .disabled(busy || preview?.status?.applied != true)
                Button("Apply") { settings.applyStatusline(profileID) }
                    .disabled(busy)
            }
            MessageLine(message: settings.message("statusline:\(profileID)"))
        } header: {
            Text("Plain `claude` (settings.json)")
        } footer: {
            VStack(alignment: .leading, spacing: 2) {
                Text("Apply makes plain `claude` in this config dir show the statusline too; a backup of settings.json is kept.")
                if let path = preview?.status?.scriptPath {
                    Text("Script: \(DirectoryField.abbreviate(path))").textSelection(.enabled)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }
}
