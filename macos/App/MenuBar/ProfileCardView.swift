import SwiftUI

/// One profile: header, status chip, limit rows, supervisor line, actions.
struct ProfileCardView: View {
    @Environment(AppModel.self) private var model
    let profile: ProfileSnapshot
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if let chip = StatusChip.make(for: profile, now: now) {
                chipView(chip)
            }
            ForEach(profile.rows) { row in
                UsageRowView(row: row, now: now)
            }
            supervisorLine
            if let message = model.message(for: profile.id) {
                Text(message.text)
                    .font(.caption)
                    .foregroundStyle(message.isError ? .red : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            actions
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    private var header: some View {
        HStack {
            Text("\(profile.emoji) \(profile.name)")
                .font(.headline)
            Spacer()
            if profile.supervisor.state == .paused {
                Label("Paused", systemImage: "pause.circle.fill")
                    .labelStyle(.titleAndIcon)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
            }
        }
    }

    @ViewBuilder
    private func chipView(_ chip: StatusChip) -> some View {
        HStack(spacing: 6) {
            Image(systemName: chip.symbol)
            Text(chip.text)
            if chip.offersSignIn {
                Spacer()
                Button("Sign in") { model.signIn(profileID: profile.id) }
                    .controlSize(.small)
                    .disabled(model.isBusy("signin:\(profile.id)"))
            }
        }
        .font(.caption)
        .foregroundStyle(chip.isWarning ? .orange : .secondary)
    }

    @ViewBuilder
    private var supervisorLine: some View {
        let sup = profile.supervisor
        VStack(alignment: .leading, spacing: 2) {
            Text("\(sup.activeSessions) ccs \(sup.activeSessions == 1 ? "session" : "sessions") · \(sup.pausedSessions) paused · \(sup.otherSessions) other")
            if sup.state == .paused, let resume = sup.resumeAt {
                Text("Resumes \(TimeFormat.compact(resume, now: now))")
            }
            if let warmup = sup.nextWarmupAt {
                Text("Next warm-up \(TimeFormat.absolute(warmup, now: now))")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private var actions: some View {
        HStack {
            Button("Warm up now") { model.warmUp(profileID: profile.id) }
                .disabled(model.isBusy("warmup:\(profile.id)"))
            Button(profile.supervisor.state == .paused ? "Resume" : "Pause") {
                model.togglePause(profile: profile)
            }
            .disabled(model.isBusy("pause:\(profile.id)"))
            Spacer()
            Button {
                model.requestSettings(profileID: profile.id)
            } label: {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.borderless)
            .help("Profile settings")
        }
        .controlSize(.small)
    }
}

/// Status chip content (presentation of the Python-provided `status`).
struct StatusChip: Equatable {
    var symbol: String
    var text: String
    var isWarning: Bool
    var offersSignIn: Bool

    static func make(for profile: ProfileSnapshot, now: Date) -> StatusChip? {
        switch profile.status {
        case .ok:
            return nil
        case .stale:
            let age = profile.updatedAt.map { "updated \(TimeFormat.ago($0, now: now))" } ?? "stale data"
            return StatusChip(symbol: "clock.badge.exclamationmark", text: age, isWarning: true, offersSignIn: false)
        case .needsSignIn:
            return StatusChip(symbol: "person.crop.circle.badge.exclamationmark", text: "Sign-in required", isWarning: true, offersSignIn: true)
        case .noSubscription:
            return StatusChip(symbol: "creditcard.trianglebadge.exclamationmark", text: "No Claude subscription limits", isWarning: false, offersSignIn: false)
        case .sourceError:
            return StatusChip(symbol: "exclamationmark.triangle", text: "Usage unavailable (see ccs doctor)", isWarning: true, offersSignIn: false)
        case .noData:
            return StatusChip(symbol: "hourglass", text: "No data yet", isWarning: false, offersSignIn: false)
        case .unknown:
            return StatusChip(symbol: "questionmark.circle", text: "Unknown status", isWarning: true, offersSignIn: false)
        }
    }
}
