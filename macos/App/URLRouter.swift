import AppKit

/// Routes `ccsupervisor://` URLs (ADR-0011) to the model; parsing lives in `DeepLink`.
@MainActor
enum URLRouter {
    static func handle(_ urls: [URL], model: AppModel = .shared) {
        for url in urls { model.open(url) }
    }
}

/// Starts the model, receives URL opens (a `MenuBarExtra` app has no window to attach
/// `.onOpenURL` to, so URLs arrive through the app delegate) and saves before quitting.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppModel.shared.start()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        URLRouter.handle(urls)
    }

    /// Every quit path (⌘Q, the menu's Quit, logout) comes through here: save pending
    /// Settings edits before the process ends.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        AppModel.shared.prepareToQuit()
        return .terminateNow
    }

    /// Belt and braces; a second run finds nothing left to save.
    func applicationWillTerminate(_ notification: Notification) {
        AppModel.shared.prepareToQuit()
    }
}
