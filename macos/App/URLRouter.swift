import AppKit

/// Routes `ccsupervisor://` URLs (ADR-0011) to the model; parsing lives in `DeepLink`.
@MainActor
enum URLRouter {
    static func handle(_ urls: [URL], model: AppModel = .shared) {
        for url in urls { model.open(url) }
    }
}

/// Starts the model and receives URL opens (a `MenuBarExtra` app has no window
/// to attach `.onOpenURL` to, so URLs arrive through the app delegate).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppModel.shared.start()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        URLRouter.handle(urls)
    }
}
