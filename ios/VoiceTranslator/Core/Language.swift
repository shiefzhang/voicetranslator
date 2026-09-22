import Foundation

/// 语种表与方向规则。逐条对齐 Android `MainActivity.codes/names/shortNames`
/// 与 `Pipeline.languageName`，数值与字符串一字不差。
///
/// 移植设计书 §3.1 / §3.2：主语言只能是 ASR 支持的 zh/ja/ko/en，
/// 法/德/俄只能作为翻译目标语言。
enum VT {
    static let codes = ["zh", "ja", "ko", "en", "fr", "de", "ru"]
    static let names = ["中文", "日本語", "한국어", "English", "Français", "Deutsch", "Русский"]
    static let shortNames = ["中文", "日语", "韩语", "英语", "法语", "德语", "俄语"]

    /// ASR 可用的主语言数量（前 4 个）。
    static let speechCodeCount = 4
}

enum Language {
    /// `MainActivity.index(code:)` —— 找不到时返回 0（与 Android 一致）。
    static func index(of code: String) -> Int {
        VT.codes.firstIndex(of: code) ?? 0
    }

    static func name(_ code: String) -> String { VT.names[index(of: code)] }
    static func shortName(_ code: String) -> String { VT.shortNames[index(of: code)] }

    /// 只能作为翻译目标、不能作为主语言的语种。
    static func isTranslationOnly(_ code: String) -> Bool {
        !VT.codes.prefix(VT.speechCodeCount).contains(code)
    }

    /// `MainActivity.reverseAvailable()` —— 目标语言是四语之一时反向页才可用。
    static func reverseAvailable(target: String) -> Bool {
        VT.codes.prefix(VT.speechCodeCount).contains(target)
    }

    /// `Pipeline.languageName` —— 送给 C++ 翻译引擎的英文名。
    static func engineName(_ code: String) -> String {
        switch code {
        case "zh": return "Chinese"
        case "ja": return "Japanese"
        case "ko": return "Korean"
        case "fr": return "French"
        case "de": return "German"
        case "ru": return "Russian"
        default: return "English"
        }
    }
}
