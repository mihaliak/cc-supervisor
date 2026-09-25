# ADR-0021: App icon: orange usage gauge

- Status: accepted
- Date: 2026-09-25
- Source: user decision (picked concept 2 "Gauge" of five orange concepts)

## Decision
- **Artwork:** a white usage gauge (a half-circle track, a progress arc at 70 %, a needle, a hub) with **CCS** below it, on an orange tile with a diagonal gradient `#FF9142 → #F2570B`. The CCS lettering is Bricolage Grotesque ExtraBold, converted to outlines.
- **Grid:** Apple's macOS icon grid, an 824 × 824 tile centered on 1024 × 1024 with a soft drop shadow.
- **Small sizes:** at 16 and 32 px the CCS text is dropped and the gauge is drawn larger, because the text can't be read there.
- **Used for:** the app icon (Finder, Settings, the widget gallery) and native notifications, which show the app icon automatically. The menu bar keeps its text label or the SF Symbol gauge (ADR-0018), which matches this icon.
- **Source of truth:** `macos/Branding/render-icon.swift`, which draws the icon with CoreGraphics. `make icon` regenerates:
  - `macos/App/Assets.xcassets/AppIcon.appiconset` (16–1024 px, committed)
  - `macos/Branding/AppIcon.svg` (master, text outlined)
  - `macos/Branding/AppIcon-1024.png`
- **Font:** `macos/Branding/fonts/BricolageGrotesque-ExtraBold.ttf` is kept for regeneration (SIL Open Font License, `OFL.txt` next to it). The app never loads it at runtime.

## Packaging notes (2026-09-25)
- The widget extension carries the same `AppIcon` asset catalog (`CFBundleIconName`) as the app, so any system lookup of the extension finds the icon too.
- `make app` restarts the widget extension process (`killall CCSupervisorWidgets`) after installing. macOS otherwise keeps running the old extension, which can't read settings added by the new version, so widgets hang on loading skeletons and Edit Widget options break.
- `CFBundleVersion` is the git commit count (`make app` passes `CURRENT_PROJECT_VERSION`), so every upgrade changes the build number.
- **Known macOS issue:** the system icon cache can keep a "generic app" icon for the bundle id from before the icon existed. It shows up in notification banners and the widget gallery, while Finder shows the right icon. Fix, once: `sudo rm -rf /Library/Caches/com.apple.iconservices.store && sudo killall -9 iconservicesd iconservicesagent; killall NotificationCenter` (the manual's troubleshooting page has it).

## Rules for implementers
- Change the icon only through `render-icon.swift` plus `make icon`. Never hand-edit the PNGs.
- Notification fallbacks sent through `osascript` (app not running) show the Script Editor icon. That's a macOS limit, not a bug.
