import SwiftUI
import UniformTypeIdentifiers

/// 设置页。对齐 Android `MainActivity.settings()`：
/// 停顿时间 → 主语言 / 翻译语言 / 分享内容 → 两个模型包 → 界面风格 → 说明 → 版本记录。
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    @State private var importingKind: ModelPackages.Kind?
    @State private var showImporter = false

    private var theme: VTTheme { model.theme }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("设置")
                .font(.system(size: 30, weight: .bold))
                .foregroundStyle(theme.ink)
                .padding(.horizontal, 24)
                .padding(.vertical, 22)

            Divider().background(theme.line)

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    pauseSection
                    Divider().background(theme.line)
                    row(title: "主语言", value: Language.name(model.mainCode)) {
                        model.requestLanguageChange(.main)
                    }
                    row(title: "翻译语言", value: Language.name(model.otherCode)) {
                        model.requestLanguageChange(.other)
                    }
                    row(title: "分享内容", value: AppModel.shareModeLabels[model.shareMode]) {
                        shareModeDialog()
                    }
                    modelRow(title: "转写模型包", kind: .asr)
                    modelRow(title: "翻译模型包", kind: .translation)
                    themeSection
                    tipText
                    Divider().background(theme.line)
                    aboutSection
                }
            }
        }
        .background(theme.background)
        .confirmationDialog(
            model.editingLanguage?.title ?? "选择语言",
            isPresented: Binding(
                get: { model.editingLanguage != nil },
                set: { if !$0 { model.editingLanguage = nil } }
            ),
            titleVisibility: .visible,
            presenting: model.editingLanguage
        ) { target in
            ForEach(languageCodes(for: target), id: \.self) { code in
                Button(Language.name(code)) {
                    model.commitLanguageChange(to: code)
                }
            }
            Button("放弃", role: .cancel) { model.editingLanguage = nil }
        } message: { target in
            Text(target == .main
                 ? "主语言只能是中文、日语、韩语或英语"
                 : "法语、德语和俄语只能作为翻译目标语言")
        }
        .confirmationDialog(
            "分享内容",
            isPresented: $showShareModeDialog,
            titleVisibility: .visible
        ) {
            ForEach(Array(AppModel.shareModeLabels.enumerated()), id: \.offset) { index, label in
                Button(label) { model.shareMode = index }
            }
            Button("取消", role: .cancel) {}
        }
        .sheet(isPresented: $model.changelogVisible) {
            changelogSheet
        }
        .fileImporter(
            isPresented: $showImporter,
            allowedContentTypes: [.vtModel, .zip, .data],
            allowsMultipleSelection: false
        ) { result in
            guard let kind = importingKind else { return }
            importingKind = nil
            switch result {
            case .success(let urls):
                guard let url = urls.first else { return }
                model.importPackage(url: url, kind: kind)
            case .failure(let error):
                model.alert = AppModel.PendingAlert(title: "导入失败", message: error.localizedDescription)
            }
        }
    }

    // MARK: - 停顿时间

    @State private var showShareModeDialog = false

    private var pauseSliderValue: Binding<Double> {
        Binding(
            get: { Double(model.pauseMs) },
            set: { model.pauseMs = Int($0.rounded()) }
        )
    }

    private var pauseSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("停顿时间")
                    .font(.system(size: 17))
                    .foregroundStyle(theme.ink)
                Spacer()
                Text(String(format: "%.1f 秒", Double(model.pauseMs) / 1000))
                    .font(.system(size: 17))
                    .foregroundStyle(theme.accent)
            }
            // Android：SeekBar max=17，取值 300…2000 ms，步长 100。
            Slider(value: pauseSliderValue, in: 300...2000, step: 100)
                .tint(theme.accent)
                .accessibilityLabel("停顿时间，0.3 到 2 秒")
        }
        .padding(.horizontal, 24)
        .padding(.top, 18)
        .padding(.bottom, 14)
    }

    // MARK: - 通用行

    private func row(title: String, value: String, action: @escaping () -> Void) -> some View {
        VStack(spacing: 0) {
            Button(action: action) {
                HStack {
                    Text(title)
                        .font(.system(size: 18))
                        .foregroundStyle(theme.ink)
                    Spacer()
                    Text(value + "  ›")
                        .font(.system(size: 16))
                        .foregroundStyle(theme.muted)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 20)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(title)，当前 \(value)")
            Divider().background(theme.line)
        }
    }

    // MARK: - 模型包

    private func modelRow(title: String, kind: ModelPackages.Kind) -> some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title)
                        .font(.system(size: 18))
                        .foregroundStyle(theme.ink)
                    Text(ModelPackages.displayName(kind))
                        .font(.system(size: 12))
                        .foregroundStyle(theme.muted)
                }
                Spacer()
                Button("选择模型包") {
                    importingKind = kind
                    showImporter = true
                }
                .font(.system(size: 15))
                .frame(width: 124, height: 48)
                .foregroundStyle(theme.accent)
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(theme.accent, lineWidth: 1))
                .disabled(model.isImporting)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 16)
            Divider().background(theme.line)
        }
    }

    // MARK: - 界面风格

    private var themeSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("界面风格")
                .font(.system(size: 18))
                .foregroundStyle(theme.ink)
            HStack(spacing: 10) {
                ForEach(VTTheme.allCases) { option in
                    Button {
                        model.theme = option
                    } label: {
                        Text(option.label)
                            .font(.system(size: 16))
                            .frame(maxWidth: .infinity, minHeight: 66)
                            .foregroundStyle(option == .dark ? Color.white : Color(hex: 0x233652))
                            .background(option.swatch)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12)
                                .stroke(model.theme == option ? theme.accent : theme.line, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 18)
        .padding(.bottom, 20)
    }

    private var tipText: some View {
        Text(model.isImporting
             ? model.status
             : "模型从本地文件导入 · 支持中日韩英\n对话仅保留本次使用，音频不保存")
            .font(.system(size: 12))
            .foregroundStyle(theme.muted)
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
    }

    // MARK: - 关于

    private var aboutSection: some View {
        VStack(spacing: 0) {
            Button {
                model.changelogVisible = true
            } label: {
                Text("查看版本更新记录")
                    .font(.system(size: 16))
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .foregroundStyle(theme.accent)
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(theme.accent, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 24)
            .padding(.top, 20)

            Text("离线语音翻译  ·  版本 \(Self.appVersion)")
                .font(.system(size: 12))
                .foregroundStyle(theme.muted)
                .frame(maxWidth: .infinity)
                .padding(.top, 14)
                .padding(.bottom, 28)
        }
    }

    /// 版本记录弹窗。刻意不用 `NavigationStack`（iOS 16+）——
    /// 移植设计书 §1 定的是 iOS 15+，且本项目其余页面都是手写头部，
    /// 这样风格一致、也不引入 deprecated 的 `NavigationView`。
    private var changelogSheet: some View {
        VStack(spacing: 0) {
            HStack {
                Text("版本更新记录")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(theme.ink)
                Spacer()
                Button("知道了") { model.changelogVisible = false }
                    .font(.system(size: 16))
                    .foregroundStyle(theme.accent)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 16)

            Divider().background(theme.line)

            ScrollView {
                Text(AppModel.changelogText)
                    .font(.system(size: 15))
                    .lineSpacing(5)
                    .foregroundStyle(theme.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(24)
                    .textSelection(.enabled)
            }
        }
        .background(theme.background)
    }

    // MARK: - 语言选项

    /// 主语言只列四语，目标语言列七语（对应 Android `language()` 的 choices）。
    private func languageCodes(for target: AppModel.LanguageTarget?) -> [String] {
        target == .main ? Array(VT.codes.prefix(VT.speechCodeCount)) : VT.codes
    }

    private func shareModeDialog() {
        showShareModeDialog = true
    }

    static var appVersion: String {
        let short = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
        return "\(short) (\(build))"
    }
}
