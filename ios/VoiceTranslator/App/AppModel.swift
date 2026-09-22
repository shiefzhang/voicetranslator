import Combine
import Foundation
import SwiftUI
import UIKit

/// 全局状态与偏好。对应 Android `MainActivity` 里的字段 + `SharedPreferences("vt")`。
///
/// 移植设计书 §2 的分层：本类是"协调层 + 状态容器"，不直接触碰推理线程，
/// 所有引擎事件都通过 `Pipeline.Listener` 回到主线程。
@MainActor
final class AppModel: ObservableObject {

    // MARK: - 偏好（对应 SharedPreferences "vt"）

    @AppStorage("main") var mainCode: String = "zh"
    @AppStorage("other") var otherCode: String = "en"
    @AppStorage("pause") var pauseMs: Int = 800
    @AppStorage("theme") private var themeRaw: String = VTTheme.fresh.rawValue
    @AppStorage("shareMode") private var shareModeRaw: Int = 2

    var theme: VTTheme {
        get { VTTheme(rawValue: themeRaw) ?? .fresh }
        set { themeRaw = newValue.rawValue }
    }

    var shareMode: Int {
        get { shareModeRaw }
        set { shareModeRaw = newValue }
    }

    static let shareModeLabels = ["转写内容", "翻译内容", "转写/翻译对照", "先转写后翻译"]
    static let maxRowsPerPage = 200

    // MARK: - 页面与运行状态

    enum Page: Int { case reverse = 0, translate = 1, settings = 2 }

    enum LanguageTarget: String, Identifiable {
        case main, other
        var id: String { rawValue }
        var title: String { self == .main ? "主语言" : "翻译语言" }
    }

    struct HistoryRow: Identifiable, Equatable {
        let key: String
        var source: String
        var translation: String
        var id: String { key }
    }

    struct PendingAlert: Identifiable {
        let id = UUID()
        var title: String?
        var message: String
        var primary: (label: String, action: () -> Void)?
        var secondary: (label: String, action: () -> Void)?
        var cancelLabel: String?
    }

    @Published var page: Page = .translate
    @Published var status = "点击开始，停顿后自动翻译"
    @Published var drafts: [String] = ["", ""]
    @Published var focusedKeys: [String] = ["", ""]
    @Published var history: [[HistoryRow]] = [[], []]
    @Published var isRecording = false
    @Published var isProcessing = false
    @Published var isImporting = false
    @Published var alert: PendingAlert?
    @Published var toast: String?
    @Published var editingLanguage: LanguageTarget?
    @Published var changelogVisible = false

    private var pipeline: Pipeline?
    private var session: Int64 = 0
    private var destroyed = false

    // MARK: - 派生状态

    /// `reverseAvailable()`：目标语言是四语之一时反向页才可用。
    var reverseAvailable: Bool { Language.reverseAvailable(target: otherCode) }

    /// `src(page)` / `dst(page)`。
    func sourceCode(_ page: Page) -> String { page == .translate ? mainCode : otherCode }
    func targetCode(_ page: Page) -> String { page == .translate ? otherCode : mainCode }

    var mainLabel: String { Language.name(mainCode) }
    var otherLabel: String { Language.name(otherCode) }

    func navLabel(_ page: Page) -> String {
        switch page {
        case .reverse: return reverseAvailable ? "译成" + Language.shortName(mainCode) : "反向不可用"
        case .translate: return "译成" + Language.shortName(otherCode)
        case .settings: return "设置"
        }
    }

    var canStart: Bool { pipeline == nil && !isImporting }

    /// 流水线是否处于空闲态（对应 Android `pipeline == null`）。
    var pipelineIsIdle: Bool { pipeline == nil }

    var recordButtonLabel: String {
        if pipeline == nil { return "开始翻译" }
        return isRecording ? "停止翻译" : "处理中…"
    }

    // MARK: - 监听翻译流水线

    /// 开始一次录音会话（对应 `MainActivity.start()`）。
    func start() {
        guard pipeline == nil else { return }

        guard AudioCapture.hasPermission else {
            AudioCapture.requestPermission { [weak self] granted in
                Task { @MainActor in
                    if granted {
                        self?.start()
                    } else {
                        self?.alert = PendingAlert(message: "需要麦克风权限才能录音，请在系统设置中允许。")
                    }
                }
            }
            return
        }

        guard let asr = ModelPackages.selected(.asr),
              let translation = ModelPackages.selected(.translation) else {
            alert = PendingAlert(
                title: "先准备模型包",
                message: "请在设置中导入转写模型包和翻译模型包，随后可完全离线使用。",
                primary: ("前往设置", { [weak self] in self?.page = .settings }),
                cancelLabel: "取消"
            )
            return
        }

        let targetPage = page
        guard history[targetPage.rawValue].count < Self.maxRowsPerPage else {
            alert = PendingAlert(message: "本页已达 \(Self.maxRowsPerPage) 句，请复制并清空后继续")
            return
        }
        guard targetPage != .settings else { return }

        session += 1
        focusedKeys[targetPage.rawValue] = ""
        UIApplication.shared.isIdleTimerDisabled = true

        do {
            let pipeline = try Pipeline(asrDirectory: asr,
                                        translationDirectory: translation,
                                        source: sourceCode(targetPage),
                                        target: targetCode(targetPage),
                                        pauseMs: pauseMs,
                                        listener: self)
            self.pipeline = pipeline
            status = "正在准备模型…"
            isProcessing = true
            pipeline.start()
        } catch {
            UIApplication.shared.isIdleTimerDisabled = false
            status = "错误：" + error.localizedDescription
            alert = PendingAlert(message: error.localizedDescription)
        }
    }

    /// 停止录音，已进队的句子继续处理（对应 `pipeline.stop()`）。
    func stopRecording() {
        pipeline?.stop()
        status = "录音已停止，正在处理剩余句子…"
        isRecording = false
    }

    func toggleRecording() {
        if pipeline != nil {
            stopRecording()
        } else {
            start()
        }
    }

    /// 页面切走时停录（对应底部导航的 `pipeline.stop()`）。
    func select(page newPage: Page) {
        if newPage == .reverse && !reverseAvailable {
            toast = "法语、德语和俄语仅支持作为翻译目标语言，暂不支持反向翻译"
            return
        }
        guard newPage != page else { return }
        pipeline?.stop()
        isRecording = false
        page = newPage
    }

    // MARK: - 文本操作

    func rows(_ page: Page) -> [HistoryRow] { history[page.rawValue] }

    func focusKey(_ page: Page) -> String { focusedKeys[page.rawValue] }

    func draft(_ page: Page) -> String { drafts[page.rawValue] }

    func focus(row: HistoryRow, on page: Page) {
        focusedKeys[page.rawValue] = row.key
    }

    func clearPage(_ page: Page) {
        guard pipeline == nil else {
            alert = PendingAlert(message: "请等待录音与翻译结束后清空")
            return
        }
        history[page.rawValue].removeAll()
        drafts[page.rawValue] = ""
        focusedKeys[page.rawValue] = ""
    }

    /// `copyAllText()` —— 复制两个翻译页的内容。
    func copyAllText() -> String {
        var out = ""
        for page in [Page.reverse, Page.translate] {
            let rows = history[page.rawValue]
            let draft = drafts[page.rawValue]
            if rows.isEmpty && draft.isEmpty { continue }
            if !out.isEmpty { out += "\n\n" }
            out += "\(Language.name(sourceCode(page))) → \(Language.name(targetCode(page)))\n"
            for row in rows {
                out += "原文：\(row.source)\n译文：\(row.translation)\n"
            }
            if !draft.isEmpty { out += "转写中：\(draft)\n" }
        }
        return out
    }

    /// `shareTextValue()` —— 四种分享模式。
    func shareTextValue() -> String {
        var out = ""
        let mode = shareMode
        for page in [Page.reverse, Page.translate] {
            let rows = history[page.rawValue]
            let draft = drafts[page.rawValue]
            if rows.isEmpty && draft.isEmpty { continue }
            if !out.isEmpty { out += "\n\n" }
            out += "\(Language.name(sourceCode(page))) → \(Language.name(targetCode(page)))\n"
            switch mode {
            case 0:
                for row in rows { out += row.source + "\n" }
            case 1:
                for row in rows { out += row.translation + "\n" }
            case 2:
                for row in rows { out += "转写：\(row.source)\n翻译：\(row.translation)\n" }
            default:
                out += "转写：\n"
                for row in rows { out += row.source + "\n" }
                out += "\n翻译：\n"
                for row in rows { out += row.translation + "\n" }
            }
            if !draft.isEmpty { out += "\n转写中：\(draft)\n" }
        }
        return out
    }

    // MARK: - 语言切换

    /// `language(key)` —— 有文本时先问是否复制并清空。
    func commitLanguageChange(to code: String) {
        guard let target = editingLanguage else { return }
        let key = target.rawValue
        let current = target == .main ? mainCode : otherCode
        let opposite = target == .main ? otherCode : mainCode

        if target == .main && Language.isTranslationOnly(code) {
            toast = "法语、德语和俄语仅支持作为翻译目标语言"
            return
        }
        if code == opposite {
            toast = "主语言与翻译语言不能相同"
            return
        }
        if code == current { return }

        editingLanguage = nil
        performChange(target: target, code: code, key: key)
    }

    private func performChange(target: LanguageTarget, code: String, key: String) {
        let hasText = history.contains { !$0.isEmpty } || drafts.contains { !$0.isEmpty }
        let apply: () -> Void = { [weak self] in
            guard let self else { return }
            for index in 0..<2 {
                self.history[index].removeAll()
                self.drafts[index] = ""
                self.focusedKeys[index] = ""
            }
            if target == .main { self.mainCode = code } else { self.otherCode = code }
        }

        guard hasText else {
            apply()
            return
        }
        alert = PendingAlert(
            title: "切换语言前处理文本",
            message: "切换到\(Language.name(code))将清空两个翻译页，可先复制全部原文和译文。",
            primary: ("复制并清空后切换", { [weak self] in
                guard let self else { return }
                UIPasteboard.general.string = self.copyAllText()
                self.toast = "已复制两个翻译页的文本"
                apply()
            }),
            secondary: ("清空并切换", apply),
            cancelLabel: "放弃"
        )
    }

    func requestLanguageChange(_ target: LanguageTarget) {
        guard pipeline == nil else {
            alert = PendingAlert(message: "请等待当前翻译结束后切换语言。")
            return
        }
        editingLanguage = target
    }

    // MARK: - 模型包导入

    func importPackage(url: URL, kind: ModelPackages.Kind) {
        guard pipeline == nil else {
            alert = PendingAlert(message: "正在处理之前的录音，请完成后再导入模型包。")
            return
        }
        isImporting = true
        status = "正在读取模型包…"
        // 这里用 `let` 强捕获，而不是 `[weak self]`：
        // `weak var self` 在并发闭包里会被判成"捕获可变变量"
        // （Swift 6 语言模式下是 error: SendableClosureCaptures）。
        // AppModel 是 @MainActor 类，本身隐式 Sendable，强捕获是安全的；
        // 而且闭包内**只**在 @MainActor 上读写 model，不存在数据竞争。
        let model = self
        Task.detached(priority: .userInitiated) {
            do {
                _ = try ModelPackages.install(from: url, kind: kind) { message in
                    Task { @MainActor in model.status = message }
                }
                await MainActor.run {
                    model.isImporting = false
                    model.status = "点击开始，停顿后自动翻译"
                    model.toast = "模型包已导入"
                }
            } catch {
                await MainActor.run {
                    model.isImporting = false
                    model.status = "点击开始，停顿后自动翻译"
                    model.alert = PendingAlert(title: "导入失败", message: error.localizedDescription)
                }
            }
        }
    }

    var changelog: String { Self.changelogText }

    static let changelogText = """
    版本 0.2.3
    • 翻译进行期间保持屏幕常亮，全部翻译完成后恢复系统息屏设置

    版本 0.2.2
    • 修复 TranslateGemma 大模型导入时的 ZIP 格式兼容问题
    • 修正反向不可用时底部导航栏的文字顺序

    版本 0.2.1
    • 转写始终跟随最新内容，翻译继续后台异步处理
    • 支持点击原文或译文切换淡黄色对应行
    • 错误语种时自动加强指令重试一次

    版本 0.2.0
    • 离线翻译模型可跨录音复用，减少重复加载等待
    • 缓存固定翻译提示词，加快连续翻译
    • 译文支持生成过程中实时显示
    • 处理积压时自动暂停录音，恢复后自动继续
    • 设置页增加版本号和版本更新记录

    版本 0.1.0
    • 首次发布
    • 支持中文、日语、韩语和英语离线语音翻译
    • 支持本地导入转写与翻译模型包
    • 支持停顿自动分句和三种界面风格
    """
}

// MARK: - Pipeline.Listener

extension AppModel: Pipeline.Listener {
    nonisolated func pipelineState(_ text: String) {
        Task { @MainActor in self.status = text }
    }

    nonisolated func pipelineDraft(_ text: String) {
        Task { @MainActor in
            guard self.page != .settings else { return }
            self.drafts[self.page.rawValue] = text
        }
    }

    nonisolated func pipelineFocus(_ id: Int64) {
        Task { @MainActor in
            guard self.page != .settings else { return }
            self.focusedKeys[self.page.rawValue] = "\(self.session):\(id)"
        }
    }

    nonisolated func pipelineSentence(id: Int64, source: String, translation: String) {
        Task { @MainActor in
            guard self.page != .settings else { return }
            let page = self.page.rawValue
            let key = "\(self.session):\(id)"
            if let index = self.history[page].firstIndex(where: { $0.key == key }) {
                self.history[page][index].source = source
                self.history[page][index].translation = translation
            } else {
                self.history[page].append(HistoryRow(key: key, source: source, translation: translation))
            }
            if self.history[page].count >= Self.maxRowsPerPage {
                self.pipeline?.stop()
            }
        }
    }

    nonisolated func pipelineDone() {
        Task { @MainActor in
            UIApplication.shared.isIdleTimerDisabled = false
            self.pipeline = nil
            self.isRecording = false
            self.isProcessing = false
            if !self.status.hasPrefix("错误") && !self.status.hasPrefix("录音错误") {
                self.status = "录音已停止 · 点击开始新的翻译"
            }
            self.drafts[self.page.rawValue] = ""
        }
    }
}
