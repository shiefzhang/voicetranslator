import SwiftUI

/// 三套界面风格，色值与 Android `MainActivity.palette()` 完全一致。
///
/// 移植设计书 §7 要求"深色模式使用语义色"；这里保留 Android 的具名色板
/// 以保证跨平台观感一致，同时由 SwiftUI 的 `preferredColorScheme` 让系统
/// 控件（弹窗、键盘）跟随所选风格。
enum VTTheme: String, CaseIterable, Identifiable {
    case dark
    case fresh
    case warm

    var id: String { rawValue }

    var label: String {
        switch self {
        case .dark: return "深色"
        case .fresh: return "清爽"
        case .warm: return "暖色"
        }
    }

    var background: Color {
        switch self {
        case .dark: return Color(hex: 0x171B24)
        case .fresh: return Color(hex: 0xFFFFFF)
        case .warm: return Color(hex: 0xFAF5EB)
        }
    }

    var ink: Color {
        switch self {
        case .dark: return Color(hex: 0xEDF2FC)
        case .fresh: return Color(hex: 0x142346)
        case .warm: return Color(hex: 0x302A22)
        }
    }

    var muted: Color {
        switch self {
        case .dark: return Color(hex: 0xAAB6CF)
        case .fresh: return Color(hex: 0x7B8CAD)
        case .warm: return Color(hex: 0x80715D)
        }
    }

    var accent: Color {
        switch self {
        case .dark: return Color(hex: 0x80ACFF)
        case .fresh: return Color(hex: 0x1468EF)
        case .warm: return Color(hex: 0x997431)
        }
    }

    var line: Color {
        switch self {
        case .dark: return Color(hex: 0x303849)
        case .fresh: return Color(hex: 0xE3EAF3)
        case .warm: return Color(hex: 0xE7DDCD)
        }
    }

    var surface: Color {
        switch self {
        case .dark: return Color(hex: 0x29354C)
        case .fresh: return Color(hex: 0xE5EFFF)
        case .warm: return Color(hex: 0xF0E4CD)
        }
    }

    /// 主题色块（设置页预览按钮用的底色）。
    var swatch: Color {
        switch self {
        case .dark: return Color(hex: 0x252B39)
        case .fresh: return Color(hex: 0xE7F1FF)
        case .warm: return Color(hex: 0xFCEDCF)
        }
    }

    /// 高亮行背景（Android `Color.rgb(255,244,179)`）。
    static let highlight = Color(hex: 0xFFF4B3)
    /// 高亮行文字（Android `Color.rgb(58,45,0)`）。
    static let highlightInk = Color(hex: 0x3A2D00)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}
