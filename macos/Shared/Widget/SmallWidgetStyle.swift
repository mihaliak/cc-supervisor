import CoreGraphics
import Foundation

/// How the small widget draws the session percentage (Edit Widget → Small widget style).
/// Stored in the widget configuration as its raw string (ADR-0019: no AppEnum/AppEntity).
public enum SmallWidgetStyle: String, Sendable, CaseIterable {
    case bar
    case gauge

    public var title: String {
        switch self {
        case .bar: "Progress bar"
        case .gauge: "Gauge"
        }
    }

    public init(configValue: String?) {
        self = configValue.flatMap(SmallWidgetStyle.init(rawValue:)) ?? .bar
    }
}

/// Geometry of the half-circle usage gauge (0 % at the left end, 100 % at the right end,
/// over the top). Angles are in degrees in SwiftUI's y-down space: 180 = left, 270 = top,
/// 360 = right.
public enum GaugeGeometry {
    public static let startDegrees: Double = 180
    public static let endDegrees: Double = 360

    /// 0…1 of the arc for a percentage (clamped).
    public static func fraction(percent: Int) -> Double {
        Double(min(max(percent, 0), 100)) / 100
    }

    /// Needle direction for a percentage.
    public static func needleDegrees(percent: Int) -> Double {
        startDegrees + (endDegrees - startDegrees) * fraction(percent: percent)
    }

    /// The needle tip for a gauge centered at `center` (y-down).
    public static func needleTip(center: CGPoint, length: CGFloat, percent: Int) -> CGPoint {
        let radians = needleDegrees(percent: percent) * .pi / 180
        return CGPoint(x: center.x + length * cos(radians), y: center.y + length * sin(radians))
    }
}
