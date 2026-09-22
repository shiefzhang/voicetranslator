import SwiftUI

/// 翻译页（正向 / 反向共用）。
/// 对齐 Android `MainActivity.translationPage()` 的信息层级：
/// 语种标题 → 译文窗格 → 原文窗格 → 状态 → 操作区。
struct TranslationView: View {
    @EnvironmentObject private var model: AppModel
    let page: AppModel.Page

    private var theme: VTTheme { model.theme }

    var body: some View {
        VStack(spacing: 0) {
            header
            translatedPane
            sourcePane
            statusBar
            actions
        }
    }

    // MARK: - 顶部语种标题

    private var header: some View {
        HStack(spacing: 8) {
            Text("\(Language.name(model.sourceCode(page)))  →  \(Language.name(model.targetCode(page)))")
                .font(.system(size: 22, weight: .bold))
                .foregroundStyle(theme.accent)
                .lineLimit(1)
                .minimumScaleFactor(0.7)

            Spacer(minLength: 0)

            Menu {
                Button("复制本页文本") {
                    UIPasteboard.general.string = copyPageText()
                    model.toast = "已复制"
                }
                Button("清空本页", role: .destructive) {
                    confirmClear()
                }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 18))
                    .foregroundStyle(theme.muted)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("复制或清空文本")
        }
        .padding(.horizontal, 22)
        .padding(.top, 14)
        .padding(.bottom, 12)
    }

    // MARK: - 双窗格

    private var translatedPane: some View {
        pane(title: "译文 · " + Language.name(model.targetCode(page)),
             titleColor: theme.accent,
             background: theme.surface,
             fontSize: 21,
             showsTranslation: true)
    }

    private var sourcePane: some View {
        pane(title: "原文 · " + Language.name(model.sourceCode(page)),
             titleColor: theme.muted,
             background: theme.background,
             fontSize: 17,
             showsTranslation: false)
    }

    private func pane(title: String,
                      titleColor: Color,
                      background: Color,
                      fontSize: CGFloat,
                      showsTranslation: Bool) -> some View {
        let rows = model.rows(page)
        let focus = model.focusKey(page)
        let draft = model.draft(page)

        return VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(titleColor)
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 6)

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        if rows.isEmpty && draft.isEmpty {
                            Text(showsTranslation ? "翻译文本将显示在这里" : "转写文本将显示在这里")
                                .font(.system(size: fontSize))
                                .foregroundStyle(theme.muted)
                        }
                        ForEach(rows) { row in
                            let text = rowText(showsTranslation ? row.translation : row.source)
                            if !text.isEmpty {
                                Text(text)
                                    .font(.system(size: fontSize))
                                    .foregroundStyle(row.key == focus ? VTTheme.highlightInk : theme.ink)
                                    .padding(.horizontal, 4)
                                    .padding(.vertical, 2)
                                    .background(row.key == focus ? VTTheme.highlight : Color.clear)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .contentShape(Rectangle())
                                    .onTapGesture { model.focus(row: row, on: page) }
                                    .id(row.key)
                                    .textSelection(.enabled)
                            }
                        }
                        if !draft.isEmpty {
                            Text(draft)
                                .font(.system(size: fontSize))
                                .foregroundStyle(theme.muted)
                                .id("draft")
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                // 焦点行变化：两个窗格都滚到该行（对应 Android 的 scrollToFocus）。
                .onChange(of: focus) { newValue in
                    guard !newValue.isEmpty else { return }
                    withAnimation(.easeInOut(duration: 0.25)) {
                        proxy.scrollTo(newValue, anchor: .center)
                    }
                }
                // 新句子到达：原文窗格跟随最新（对应 scrollSourceToLatest）。
                .onChange(of: rows.count) { _ in
                    guard showsTranslation == false, let last = rows.last else { return }
                    withAnimation(.easeInOut(duration: 0.2)) {
                        proxy.scrollTo(last.key, anchor: .bottom)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(background)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(theme.line, lineWidth: 1))
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
    }

    // MARK: - 状态与操作

    private var statusBar: some View {
        Text(model.status)
            .font(.system(size: 13))
            .foregroundStyle(theme.muted)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
    }

    private var actions: some View {
        HStack(spacing: 10) {
            Button {
                model.toggleRecording()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: model.pipelineIsIdle ? "mic.fill" : "pause.fill")
                    Text(model.recordButtonLabel)
                        .font(.system(size: 19))
                }
                .frame(maxWidth: .infinity, minHeight: 56)
                .foregroundStyle(model.canStart ? Color.white : theme.accent)
                .background(model.canStart ? theme.accent : theme.background)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12)
                    .stroke(model.canStart ? Color.clear : theme.accent, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .disabled(model.isImporting)
            .accessibilityLabel(model.recordButtonLabel)

            Button {
                share()
            } label: {
                Text("分享")
                    .font(.system(size: 16))
                    .frame(width: 100, height: 56)
                    .foregroundStyle(theme.accent)
                    .background(theme.background)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(theme.accent, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("分享转写和翻译文本")
        }
        .padding(.horizontal, 22)
        .padding(.top, 4)
        .padding(.bottom, 24)
    }

    // MARK: - 工具

    /// 对应 Android `rowText`：把行内换行折成空格。
    private func rowText(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func copyPageText() -> String {
        let rows = model.rows(page)
        let translated = rows.map { rowText($0.translation) }.joined(separator: "\n")
        let source = rows.map { rowText($0.source) }.joined(separator: "\n")
        return translated + "\n\n" + source
    }

    private func confirmClear() {
        guard model.pipelineIsIdle else {
            model.alert = AppModel.PendingAlert(message: "请等待录音与翻译结束后清空")
            return
        }
        model.alert = AppModel.PendingAlert(
            message: "清空本页所有文本？",
            primary: ("清空", { model.clearPage(page) }),
            cancelLabel: "取消"
        )
    }

    private func share() {
        let value = model.shareTextValue().trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            model.alert = AppModel.PendingAlert(message: "暂无可分享的内容")
            return
        }
        let stamp = DateFormatter()
        stamp.dateFormat = "yyyyMMdd_HHmmss"
        let name = "share_\(stamp.string(from: Date())).txt"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do {
            try value.write(to: url, atomically: true, encoding: .utf8)
            SharePresenter.present(activityItems: [url])
        } catch {
            model.alert = AppModel.PendingAlert(message: "分享失败：" + error.localizedDescription)
        }
    }
}

/// 分享面板桥接（对应 Android 的 `ACTION_SEND` + `FileProvider`）。
enum SharePresenter {
    static func present(activityItems: [Any]) {
        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive }),
            let root = scene.keyWindow?.rootViewController else { return }

        var top = root
        while let presented = top.presentedViewController { top = presented }

        let controller = UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
        if let popover = controller.popoverPresentationController {
            popover.sourceView = top.view
            popover.sourceRect = CGRect(x: top.view.bounds.midX, y: top.view.bounds.maxY, width: 0, height: 0)
        }
        top.present(controller, animated: true)
    }
}
