import SwiftUI

/// App 入口。对应 Android 的 `MainActivity` + `AndroidManifest.xml`。
///
/// 这里只做三件事：
/// 1. 建唯一的 `AppModel`（相当于 Android 的唯一 Activity 里那堆字段）；
/// 2. 把 `RootView` 挂上去（三页导航在 `RootView` 内部）；
/// 3. 处理生命周期 —— **设计书 §1 明确不承诺后台/锁屏持续录音**，
///    因此退到非 active 就立刻停录。已进入队列的句子继续在
///    `vt-translation` 线程上跑完（与 Android 的行为一致）。
@main
struct VoiceTranslatorApp: App {

    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
        }
        .onChange(of: scenePhase) { phase in
            switch phase {
            case .active:
                break
            default:
                // 后台 / 非活跃：停止采集与 ASR，翻译线程自然排空后回到空闲。
                if !model.pipelineIsIdle {
                    model.stopRecording()
                }
            }
        }
    }
}
