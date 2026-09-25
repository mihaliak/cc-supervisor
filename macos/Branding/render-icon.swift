#!/usr/bin/env swift
// Renders the CC Supervisor "Gauge" app icon (ADR-0021).
//
//   swift macos/Branding/render-icon.swift        (run from the repo root; `make icon`)
//
// Writes:
//   macos/App/Assets.xcassets/AppIcon.appiconset/*.png + Contents.json   (16–1024 px)
//   macos/Branding/AppIcon.svg                                            (master, text outlined)
//   macos/Branding/AppIcon-1024.png                                       (preview)
//
// Artwork is defined on a 100×100 tile (the concept's units) and placed on Apple's macOS
// icon grid: an 824×824 tile centered on a 1024×1024 canvas, with a soft drop shadow.
// Sizes of 32 px and below drop the "CCS" text and draw a larger gauge.

import AppKit
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let brandingDir = root.appendingPathComponent("macos/Branding")
let iconsetDir = root.appendingPathComponent("macos/App/Assets.xcassets/AppIcon.appiconset")
let fontURL = brandingDir.appendingPathComponent("fonts/BricolageGrotesque-ExtraBold.ttf")

// MARK: - Palette and geometry (tile units, y down)

let orangeLight = CGColor(srgbRed: 1.0, green: 0x91 / 255.0, blue: 0x42 / 255.0, alpha: 1)   // #FF9142
let orangeDeep = CGColor(srgbRed: 0xF2 / 255.0, green: 0x57 / 255.0, blue: 0x0B / 255.0, alpha: 1) // #F2570B
let white = CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 1)
let track = CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 0.35)

let canvas: CGFloat = 1024
let tileSize: CGFloat = 824
let tileOrigin: CGFloat = (canvas - tileSize) / 2   // 100
let tileRadius: CGFloat = 22.5                       // tile units (≈185 px at 1024)

struct Gauge {
    var center: CGPoint
    var radius: CGFloat
    var stroke: CGFloat
    var needleLength: CGFloat
    var needleWidth: CGFloat
    var hub: CGFloat
    var showText: Bool
}

/// Needle and progress end at 70 % of the half circle (54° above the right horizontal).
let progressAngle: CGFloat = 54 * .pi / 180

let regular = Gauge(center: CGPoint(x: 50, y: 60), radius: 28, stroke: 8, needleLength: 20, needleWidth: 4.5, hub: 5, showText: true)
let small = Gauge(center: CGPoint(x: 50, y: 63), radius: 31, stroke: 11, needleLength: 22, needleWidth: 6.5, hub: 7, showText: false)

// MARK: - Text outline ("CCS", Bricolage Grotesque ExtraBold)

func textPath(_ string: String, fontSize: CGFloat, tracking: CGFloat, centerX: CGFloat, baseline: CGFloat) -> CGPath {
    guard let descriptors = CTFontManagerCreateFontDescriptorsFromURL(fontURL as CFURL) as? [CTFontDescriptor],
          let descriptor = descriptors.first else {
        fatalError("font not found: \(fontURL.path)")
    }
    let font = CTFontCreateWithFontDescriptor(descriptor, fontSize, nil)
    let attributes: [NSAttributedString.Key: Any] = [
        NSAttributedString.Key(kCTFontAttributeName as String): font,
        NSAttributedString.Key(kCTKernAttributeName as String): tracking,
    ]
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: string, attributes: attributes))
    // Tracking is added after every glyph; drop the trailing one when centering.
    let width = CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil)) - tracking
    let originX = centerX - width / 2

    let path = CGMutablePath()
    for run in CTLineGetGlyphRuns(line) as! [CTRun] {
        let count = CTRunGetGlyphCount(run)
        var glyphs = [CGGlyph](repeating: 0, count: count)
        var positions = [CGPoint](repeating: .zero, count: count)
        CTRunGetGlyphs(run, CFRange(location: 0, length: count), &glyphs)
        CTRunGetPositions(run, CFRange(location: 0, length: count), &positions)
        for i in 0..<count {
            guard let glyph = CTFontCreatePathForGlyph(font, glyphs[i], nil) else { continue }
            // Font space is y-up; the tile is y-down.
            var t = CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: originX + positions[i].x, ty: baseline)
            if let placed = glyph.copy(using: &t) { path.addPath(placed) }
        }
    }
    return path
}

let ccsText = textPath("CCS", fontSize: 13, tracking: 1.4, centerX: 50, baseline: 84)

// MARK: - Shapes (tile units)

func point(on gauge: Gauge, angle: CGFloat, radius: CGFloat) -> CGPoint {
    CGPoint(x: gauge.center.x + radius * cos(angle), y: gauge.center.y - radius * sin(angle))
}

/// The arc from the left end (180°) over the top to `angle`, as a stroked outline.
func arcPath(_ gauge: Gauge, to angle: CGFloat) -> CGPath {
    let path = CGMutablePath()
    // y-down space: angles run clockwise, so go from π to (π - sweep) … expressed with the
    // mirrored angle -angle and clockwise: false in flipped coordinates.
    path.addArc(center: gauge.center, radius: gauge.radius, startAngle: .pi, endAngle: 2 * .pi - angle, clockwise: false)
    return path
}

// MARK: - Rendering

func drawIcon(in ctx: CGContext, pixels: CGFloat, gauge: Gauge) {
    let scale = pixels / canvas
    ctx.saveGState()
    // Canvas: y-down, 1024 units.
    ctx.translateBy(x: 0, y: pixels)
    ctx.scaleBy(x: scale, y: -scale)

    // Drop shadow under the tile (Apple's macOS icon template: soft, slightly below).
    let tileRect = CGRect(x: tileOrigin, y: tileOrigin, width: tileSize, height: tileSize)
    let tilePath = CGPath(roundedRect: tileRect, cornerWidth: tileRadius * tileSize / 100, cornerHeight: tileRadius * tileSize / 100, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * scale), blur: 24 * scale, color: CGColor(srgbRed: 0.18, green: 0.08, blue: 0.02, alpha: 0.32))
    ctx.addPath(tilePath)
    ctx.setFillColor(orangeDeep)
    ctx.fillPath()
    ctx.restoreGState()

    // Tile gradient (top-left light → bottom-right deep).
    ctx.saveGState()
    ctx.addPath(tilePath)
    ctx.clip()
    let gradient = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB), colors: [orangeLight, orangeDeep] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: tileRect.minX, y: tileRect.minY), end: CGPoint(x: tileRect.maxX, y: tileRect.maxY), options: [])
    ctx.restoreGState()

    // Artwork in tile units.
    ctx.translateBy(x: tileOrigin, y: tileOrigin)
    ctx.scaleBy(x: tileSize / 100, y: tileSize / 100)
    ctx.setLineCap(.round)

    ctx.addPath(arcPath(gauge, to: 0))
    ctx.setStrokeColor(track)
    ctx.setLineWidth(gauge.stroke)
    ctx.strokePath()

    ctx.addPath(arcPath(gauge, to: progressAngle))
    ctx.setStrokeColor(white)
    ctx.strokePath()

    let tip = point(on: gauge, angle: progressAngle, radius: gauge.needleLength)
    ctx.move(to: gauge.center)
    ctx.addLine(to: tip)
    ctx.setLineWidth(gauge.needleWidth)
    ctx.strokePath()

    ctx.setFillColor(white)
    ctx.fillEllipse(in: CGRect(x: gauge.center.x - gauge.hub, y: gauge.center.y - gauge.hub, width: gauge.hub * 2, height: gauge.hub * 2))

    if gauge.showText {
        ctx.addPath(ccsText)
        ctx.fillPath()
    }
    ctx.restoreGState()
}

func renderPNG(pixels: Int, to url: URL) throws {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    guard let ctx = CGContext(data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0,
                              space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw NSError(domain: "icon", code: 1)
    }
    ctx.interpolationQuality = .high
    ctx.setShouldAntialias(true)
    drawIcon(in: ctx, pixels: CGFloat(pixels), gauge: pixels <= 32 ? small : regular)
    guard let image = ctx.makeImage(),
          let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        throw NSError(domain: "icon", code: 2)
    }
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { throw NSError(domain: "icon", code: 3) }
}

// MARK: - SVG master

func svgPathData(_ path: CGPath) -> String {
    var d = ""
    func f(_ v: CGFloat) -> String { String(format: "%.2f", v).replacingOccurrences(of: ".00", with: "") }
    path.applyWithBlock { element in
        let p = element.pointee.points
        switch element.pointee.type {
        case .moveToPoint: d += "M\(f(p[0].x)) \(f(p[0].y))"
        case .addLineToPoint: d += "L\(f(p[0].x)) \(f(p[0].y))"
        case .addQuadCurveToPoint: d += "Q\(f(p[0].x)) \(f(p[0].y)) \(f(p[1].x)) \(f(p[1].y))"
        case .addCurveToPoint: d += "C\(f(p[0].x)) \(f(p[0].y)) \(f(p[1].x)) \(f(p[1].y)) \(f(p[2].x)) \(f(p[2].y))"
        case .closeSubpath: d += "Z"
        @unknown default: break
        }
    }
    return d
}

func svg() -> String {
    let g = regular
    let tip = point(on: g, angle: progressAngle, radius: g.needleLength)
    let end = point(on: g, angle: progressAngle, radius: g.radius)
    let r = tileRadius * tileSize / 100
    return """
    <svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
      <!-- CC Supervisor app icon, "Gauge" (ADR-0021). Generated by macos/Branding/render-icon.swift. -->
      <defs>
        <linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#FF9142"/>
          <stop offset="1" stop-color="#F2570B"/>
        </linearGradient>
        <filter id="shadow" x="-10%" y="-10%" width="120%" height="125%">
          <feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#2E1405" flood-opacity="0.32"/>
        </filter>
      </defs>
      <rect x="\(Int(tileOrigin))" y="\(Int(tileOrigin))" width="\(Int(tileSize))" height="\(Int(tileSize))" rx="\(String(format: "%.1f", r))" fill="url(#tile)" filter="url(#shadow)"/>
      <g transform="translate(\(Int(tileOrigin)) \(Int(tileOrigin))) scale(\(String(format: "%.2f", tileSize / 100)))" fill="none" stroke-linecap="round">
        <path d="M\(g.center.x - g.radius) \(g.center.y) A\(g.radius) \(g.radius) 0 0 1 \(g.center.x + g.radius) \(g.center.y)" stroke="#FFFFFF" stroke-opacity="0.35" stroke-width="\(g.stroke)"/>
        <path d="M\(g.center.x - g.radius) \(g.center.y) A\(g.radius) \(g.radius) 0 0 1 \(String(format: "%.2f", end.x)) \(String(format: "%.2f", end.y))" stroke="#FFFFFF" stroke-width="\(g.stroke)"/>
        <line x1="\(g.center.x)" y1="\(g.center.y)" x2="\(String(format: "%.2f", tip.x))" y2="\(String(format: "%.2f", tip.y))" stroke="#FFFFFF" stroke-width="\(g.needleWidth)"/>
        <circle cx="\(g.center.x)" cy="\(g.center.y)" r="\(g.hub)" fill="#FFFFFF"/>
        <path d="\(svgPathData(ccsText))" fill="#FFFFFF" stroke="none"/>
      </g>
    </svg>

    """
}

// MARK: - Main

let slots: [(size: Int, scale: Int)] = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]

try FileManager.default.createDirectory(at: iconsetDir, withIntermediateDirectories: true)
var images: [[String: String]] = []
for slot in slots {
    let name = "icon_\(slot.size)x\(slot.size)\(slot.scale == 2 ? "@2x" : "").png"
    try renderPNG(pixels: slot.size * slot.scale, to: iconsetDir.appendingPathComponent(name))
    images.append(["filename": name, "idiom": "mac", "scale": "\(slot.scale)x", "size": "\(slot.size)x\(slot.size)"])
}
let contents: [String: Any] = ["images": images, "info": ["author": "xcode", "version": 1]]
let json = try JSONSerialization.data(withJSONObject: contents, options: [.prettyPrinted, .sortedKeys])
try json.write(to: iconsetDir.appendingPathComponent("Contents.json"))
let catalogContents = iconsetDir.deletingLastPathComponent().appendingPathComponent("Contents.json")
try JSONSerialization.data(withJSONObject: ["info": ["author": "xcode", "version": 1]], options: [.prettyPrinted, .sortedKeys])
    .write(to: catalogContents)

try svg().write(to: brandingDir.appendingPathComponent("AppIcon.svg"), atomically: true, encoding: .utf8)
try renderPNG(pixels: 1024, to: brandingDir.appendingPathComponent("AppIcon-1024.png"))
print("icon: wrote \(slots.count) PNGs to \(iconsetDir.path), AppIcon.svg and AppIcon-1024.png")
