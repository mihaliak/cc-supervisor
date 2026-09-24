import SwiftUI

@main
struct CCSupervisorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model: AppModel
    @State private var settings: SettingsController

    init() {
        // `--register-login-item` / `--unregister-login-item` (make install/uninstall) exit here.
        LoginItemController.handleLaunchArguments()
        _model = State(initialValue: AppModel.shared)
        _settings = State(initialValue: SettingsController(app: AppModel.shared))
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContentView()
                .environment(model)
        } label: {
            MenuLabelView()
                .environment(model)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environment(model)
                .environment(settings)
        }
    }
}
