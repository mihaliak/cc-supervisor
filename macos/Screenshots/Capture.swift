import AppKit
import SwiftUI

/// Draws controls with the active (focused-window) look, without activating the process:
/// the harness must never take focus from whatever the user is doing.
final class ActiveApplication: NSApplication {
    override var isActive: Bool { true }
}

/// An offscreen window that draws as the key window.
final class CaptureWindow: NSWindow {
    override var isKeyWindow: Bool { true }
    override var isMainWindow: Bool { true }
    override var canBecomeKey: Bool { true }
}

enum Appearance: String, CaseIterable {
    case light, dark

    var nsAppearance: NSAppearance {
        NSAppearance(named: self == .dark ? .darkAqua : .aqua)!
    }

    var colorScheme: ColorScheme { self == .dark ? .dark : .light }
}

/// Offscreen rendering. Pure SwiftUI goes through `ImageRenderer`; views with AppKit-backed
/// controls (buttons, toggles, fields, pickers) need a real window and `cacheDisplay`.
@MainActor
enum Capture {
    nonisolated static let scale: CGFloat = 2

    /// Host `view` in an offscreen window, let async work (e.g. `ccs` calls) settle, draw it.
    static func window<V: View>(
        _ view: V,
        size: CGSize? = nil,
        appearance: Appearance,
        settle: TimeInterval = 0.4,
        until ready: (() -> Bool)? = nil
    ) -> CGImage {
        let host = NSHostingView(rootView: view)
        host.appearance = appearance.nsAppearance
        let fitting = size ?? host.fittingSize
        host.frame = NSRect(origin: .zero, size: fitting)
        let window = CaptureWindow(
            contentRect: NSRect(x: -30000, y: -30000, width: fitting.width, height: fitting.height),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.appearance = appearance.nsAppearance
        window.backgroundColor = .windowBackgroundColor
        window.contentView = host
        window.orderFrontRegardless()
        spin(settle)
        if let ready {
            let deadline = Date().addingTimeInterval(20)
            while !ready(), Date() < deadline { spin(0.1) }
            spin(0.3)
        }
        if size == nil {
            let refit = host.fittingSize
            if refit != host.frame.size {
                window.setContentSize(refit)
                host.frame = NSRect(origin: .zero, size: refit)
                spin(0.2)
            }
        }
        host.layoutSubtreeIfNeeded()
        host.displayIfNeeded()
        let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        return rep.cgImage!
    }

    /// Pure SwiftUI content (widgets, window chrome, terminal text).
    static func swiftUI<V: View>(_ view: V, appearance: Appearance) -> CGImage {
        var image: CGImage?
        appearance.nsAppearance.performAsCurrentDrawingAppearance {
            let renderer = ImageRenderer(content: view.environment(\.colorScheme, appearance.colorScheme))
            renderer.scale = scale
            image = renderer.cgImage
        }
        return image!
    }

    static func spin(_ seconds: TimeInterval) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }

    /// Every shot sits on an opaque background: store 8-bit sRGB without alpha (renders come
    /// out as 16-bit RGBA, about 4× the file size).
    static func write(_ image: CGImage, to url: URL) {
        let context = CGContext(
            data: nil, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpace(name: CGColorSpace.sRGB)!,
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        )!
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: image.width, height: image.height))
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        let rep = NSBitmapImageRep(cgImage: context.makeImage()!)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: url)
        print("wrote \(url.lastPathComponent) (\(image.width)×\(image.height))")
    }
}

extension Image {
    /// A captured image at its point size.
    init(captured: CGImage) {
        self.init(decorative: captured, scale: Capture.scale)
    }
}
