import SwiftUI

/// The menu bar dropdown (`.window` style, ADR-0011).
struct MenuContentView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            content(now: context.date)
        }
        .frame(width: 360)
        .onAppear { model.menuOpened() }
    }

    @ViewBuilder
    private func content(now: Date) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            header(now: now)
            DaemonBannerView(state: model.banner)
            if let snapshot = model.snapshot, !snapshot.profiles.isEmpty {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(snapshot.profiles) { profile in
                            ProfileCardView(profile: profile, now: now)
                        }
                    }
                }
                .frame(maxHeight: 560)
                .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(model.snapshotStore.lastError == nil ? "No profiles yet." : "No usage data yet.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 12)
            }
            if let message = model.globalMessage {
                Text(message.text)
                    .font(.caption)
                    .foregroundStyle(message.isError ? .red : .secondary)
            }
            Divider()
            footer
        }
        .padding(12)
    }

    private func header(now: Date) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text("CC Supervisor")
                .font(.headline)
            Spacer()
            if let generated = model.snapshot?.generatedAt {
                Text("updated \(TimeFormat.ago(generated, now: now))")
                    .font(.caption)
                    .foregroundStyle(model.snapshot?.isDaemonOffline(now: now) == true ? .orange : .secondary)
            }
        }
    }

    private var footer: some View {
        HStack {
            Button {
                model.refresh()
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(model.isBusy("refresh"))
            Spacer()
            Button("Settings…") { model.requestSettings() }
            Button("Quit") { NSApp.terminate(nil) }
        }
        .controlSize(.small)
    }
}
