import Foundation
import os
import UserNotifications

/// Posts daemon events as native notifications, verbatim (ADR-0015: title/body are
/// built in Python; Swift never composes text). Clicking opens the profile link.
@MainActor
final class NotificationRouter: NSObject, UNUserNotificationCenterDelegate {
    private let center: UNUserNotificationCenter
    private let log = Logger(subsystem: "local.ccsupervisor.app", category: "notifications")
    private let openURL: @MainActor (URL) -> Void

    init(center: UNUserNotificationCenter = .current(), openURL: @escaping @MainActor (URL) -> Void) {
        self.center = center
        self.openURL = openURL
        super.init()
    }

    func start() {
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { [log] granted, error in
            if let error {
                log.error("notification authorization failed: \(error.localizedDescription, privacy: .public)")
            } else if !granted {
                log.info("notifications not allowed by the user")
            }
        }
    }

    /// The notification request for an event, or nil when it must not be posted.
    nonisolated static func request(for event: DaemonEvent) -> UNNotificationRequest? {
        guard event.data?.notify == true,
              let title = event.data?.title, !title.isEmpty,
              let body = event.data?.body
        else { return nil }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if let profile = event.profileId, !profile.isEmpty {
            content.threadIdentifier = profile
            // An id that isn't a valid profile id gets no click-through link.
            if let url = DeepLink.profile(profile).url {
                content.userInfo = ["url": url.absoluteString]
            }
        }
        let identifier = (event.key?.isEmpty == false ? event.key : nil) ?? UUID().uuidString
        return UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
    }

    func post(_ event: DaemonEvent) {
        guard let request = Self.request(for: event) else { return }
        center.add(request) { [log] error in
            if let error { log.error("post failed: \(error.localizedDescription, privacy: .public)") }
        }
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        guard let raw = response.notification.request.content.userInfo["url"] as? String,
              let url = URL(string: raw)
        else { return }
        await MainActor.run { self.openURL(url) }
    }
}
