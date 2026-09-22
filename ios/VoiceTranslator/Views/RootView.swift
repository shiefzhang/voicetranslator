import SwiftUI
import UniformTypeIdentifiers

extension UTType {
    /// 模型包扩展名。Android 用 `.vtmodel`，这里注册为可导入类型。
    static var vtModel: UTType {
        UTType(filenameExtension: "vtmodel") ?? .data
    }
}

/// 根视图：内容区 + 底部三导航。
/// 对齐 Android `MainActivity.render()` 的布局与导航语义。
struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch model.page {
                case .settings:
                    SettingsView()
                default:
                    TranslationView(page: model.page)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Divider().background(model.theme.line)
            bottomNavigation
        }
        .background(model.theme.background.ignoresSafeArea())
        .preferredColorScheme(model.theme == .dark ? .dark : .light)
        .overlay(alignment: .bottom) { toastView }
        .alert(item: $model.alert) { pending in
            alert(for: pending)
        }
    }

    private func alert(for pending: AppModel.PendingAlert) -> Alert {
        let title = Text(pending.title ?? "")

        if let primary = pending.primary, let secondary = pending.secondary {
            return Alert(title: title,
                         message: Text(pending.message),
                         primaryButton: .default(Text(primary.label), action: primary.action),
                         secondaryButton: .default(Text(secondary.label), action: secondary.action))
        }
        if let primary = pending.primary {
            return Alert(title: title,
                         message: Text(pending.message),
                         primaryButton: .default(Text(primary.label), action: primary.action),
                         secondaryButton: .cancel(Text(pending.cancelLabel ?? "取消")))
        }
        return Alert(title: title,
                     message: Text(pending.message),
                     dismissButton: .default(Text(pending.cancelLabel ?? "知道了")))
    }

    // MARK: - 底部导航

    private var bottomNavigation: some View {
        HStack(spacing: 0) {
            ForEach([AppModel.Page.reverse, .translate, .settings], id: \.rawValue) { item in
                navItem(item)
            }
        }
        .padding(.vertical, 5)
        .frame(height: 70)
        .background(model.theme.background)
    }

    private func navItem(_ item: AppModel.Page) -> some View {
        let label = model.navLabel(item)
        let selected = model.page == item
        let disabled = item == .reverse && !model.reverseAvailable
        return Button {
            model.select(page: item)
        } label: {
            VStack(spacing: 4) {
                Image(systemName: symbol(for: item))
                    .font(.system(size: 20, weight: .regular))
                    .foregroundStyle(selected ? model.theme.accent : model.theme.muted)
                Text(label)
                    .font(.system(size: 12))
                    .foregroundStyle(selected ? model.theme.accent : model.theme.muted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityHint(disabled ? "法语、德语和俄语仅支持作为翻译目标语言" : "")
    }

    private func symbol(for item: AppModel.Page) -> String {
        switch item {
        case .reverse: return "arrow.uturn.backward"
        case .translate: return "paperplane"
        case .settings: return "gearshape"
        }
    }

    private var toastView: some View {
        Group {
            if let toast = model.toast {
                Text(toast)
                    .font(.system(size: 13))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Color.black.opacity(0.8))
                    .clipShape(Capsule())
                    .padding(.bottom, 90)
                    .transition(.opacity)
                    .onAppear {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                            model.toast = nil
                        }
                    }
            }
        }
        .animation(.easeInOut(duration: 0.2), value: model.toast)
    }
}
