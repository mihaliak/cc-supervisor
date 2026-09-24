import SwiftUI

@main
struct CCSupervisorApp: App {
    var body: some Scene {
        MenuBarExtra("CC Supervisor", systemImage: "gauge.with.dots.needle.33percent") {
            Text("Hello")
                .padding()
        }
        .menuBarExtraStyle(.window)
    }
}
