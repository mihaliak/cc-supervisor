import SwiftUI

/// Warm-up editor (ADR-0010): model, prompt, triggers, schedule, active hours, cooldown.
struct WarmupSection: View {
    @Environment(SettingsController.self) private var settings
    let profileID: String

    private static let modelSuggestions = ["haiku", "sonnet", "opus", "fable"]
    private static let newEntry = JSONValue.object([
        "time": .string("06:00"),
        "weekdays": .array(["mon", "tue", "wed", "thu", "fri"].map { .string($0) }),
    ])

    var body: some View {
        let store = settings.store
        let p = { (key: String) in FieldPath.profile(profileID, key) }
        let scheduleField = p("warmup.triggers.schedule")
        let schedule = store.value(scheduleField)?.arrayValue ?? []
        let enabled = store.bool(p("warmup.enabled")) ?? true

        Section {
            Toggle("Start session windows early", isOn: store.boolBinding(p("warmup.enabled"), fallback: true))
            LabeledContent("Model") {
                HStack(spacing: 6) {
                    TextField("Model", text: store.stringBinding(p("warmup.model")))
                        .labelsHidden()
                        .frame(maxWidth: 120)
                    Menu {
                        ForEach(Self.modelSuggestions, id: \.self) { model in
                            Button(model) { store.set(p("warmup.model"), .string(model)) }
                        }
                    } label: {
                        Image(systemName: "chevron.up.chevron.down")
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                    .help("Suggestions")
                }
            }
            IssueText(messages: store.issues(p("warmup.model")))
            TextField("Prompt", text: store.stringBinding(p("warmup.prompt")))
            IssueText(messages: store.issues(p("warmup.prompt")))
        } header: {
            Text("Warm-up")
        } footer: {
            Text("A tiny prompt that starts the 5-hour window, so it resets earlier. Skipped while a window is active.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }

        Section("When") {
            Toggle("When CC Supervisor starts", isOn: store.boolBinding(p("warmup.triggers.app_start"), fallback: true))
            Toggle("On screen unlock or wake", isOn: store.boolBinding(p("warmup.triggers.unlock_wake"), fallback: true))
            Toggle("Right after a window resets", isOn: store.boolBinding(p("warmup.triggers.auto_chain"), fallback: true))
            LabeledContent("Active hours") {
                HStack(spacing: 6) {
                    DatePicker("From", selection: store.timeBinding(p("warmup.active_hours.start"), fallback: "07:00"), displayedComponents: .hourAndMinute)
                        .labelsHidden()
                    Text("–")
                    DatePicker("To", selection: store.timeBinding(p("warmup.active_hours.end"), fallback: "23:00"), displayedComponents: .hourAndMinute)
                        .labelsHidden()
                }
            }
            IssueText(messages: store.errors.messages(within: p("warmup.active_hours")))
            LabeledContent("Cooldown") {
                HStack(spacing: 4) {
                    TextField("Cooldown", value: store.intBinding(p("warmup.cooldown_minutes")), format: .number)
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .frame(width: 40)
                    Text("min").foregroundStyle(.secondary)
                    Stepper("Cooldown", value: store.intBinding(p("warmup.cooldown_minutes")), in: 1...120)
                        .labelsHidden()
                }
            }
            IssueText(messages: store.issues(p("warmup.cooldown_minutes")))
        }
        .disabled(!enabled)

        Section {
            if schedule.isEmpty {
                Text("No scheduled times").foregroundStyle(.secondary)
            }
            ForEach(Array(schedule.enumerated()), id: \.offset) { index, entry in
                ScheduleRow(
                    entry: entry,
                    onChange: { updated in
                        var list = schedule
                        list[index] = updated
                        store.set(scheduleField, .array(list))
                    },
                    onRemove: {
                        var list = schedule
                        list.remove(at: index)
                        store.set(scheduleField, .array(list))
                    }
                )
                IssueText(messages: store.errors.messages(within: p("warmup.triggers.schedule[\(index)]")))
            }
            IssueText(messages: store.issues(scheduleField))
        } header: {
            HStack {
                Text("Schedule")
                Spacer()
                Button {
                    store.set(scheduleField, .array(schedule + [Self.newEntry]))
                } label: {
                    Label("Add Time", systemImage: "plus")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
            }
        }
        .disabled(!enabled)

        Section("Status") {
            let info = settings.warmupInfo[profileID]
            LabeledContent("Next warm-up", value: info?.nextAt.map { TimeFormat.compact($0, now: Date()) } ?? "–")
            LabeledContent("Last attempt", value: lastAttemptText(info))
        }
    }

    private func lastAttemptText(_ info: WarmupInfo?) -> String {
        guard let info, let at = info.lastAttemptAt else { return "–" }
        let now = Date()
        var text = "\(TimeFormat.absolute(at, now: now)) (\(TimeFormat.ago(at, now: now)))"
        if let result = info.lastResult { text += " · \(result)" }
        if let reason = info.lastReason { text += " (\(reason.replacingOccurrences(of: "_", with: " ")))" }
        return text
    }
}

/// One schedule entry: time picker, weekday chips, remove. Unknown keys in the entry are kept.
private struct ScheduleRow: View {
    let entry: JSONValue
    let onChange: (JSONValue) -> Void
    let onRemove: () -> Void

    var body: some View {
        let time = entry["time"]?.stringValue ?? "06:00"
        let days = entry["weekdays"]?.arrayValue?.compactMap(\.stringValue) ?? []
        HStack(spacing: 8) {
            DatePicker(
                "Time",
                selection: Binding(
                    get: { TimeOfDay.date(from: time) ?? TimeOfDay.date(from: "06:00") ?? Date() },
                    set: { newDate in
                        var updated = entry
                        updated.setValue(.string(TimeOfDay.string(from: newDate)), at: [.key("time")])
                        onChange(updated)
                    }
                ),
                displayedComponents: .hourAndMinute
            )
            .labelsHidden()
            WeekdayChips(selected: days) { newDays in
                var updated = entry
                updated.setValue(.array(newDays.map { .string($0) }), at: [.key("weekdays")])
                onChange(updated)
            }
            Spacer()
            Button(role: .destructive, action: onRemove) {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Remove this time")
        }
    }
}
