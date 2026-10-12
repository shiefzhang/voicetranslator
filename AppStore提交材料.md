# App Store 提交材料 · VoiceTranslator

> 生成于 2026-10-12。凡带 `<尖括号>` 的都是**必须你替换**的占位符。
> 配套评估见 `AppStore上架可行性与操作清单.html`。

---

## 1. 审核备注（App Review Information → Notes）

**直接整段粘贴。** 这是方案 B 的核心：让审核员在碰到「缺通用模型包」之前就明白哪些语向开箱可用，不至于把「需要导入一个可选模型」判成功能不完整。

```
This app performs 100% on-device speech translation. It has no network access
at all — no account, no login, no server, no analytics.

WHAT WORKS OUT OF THE BOX
Chinese<->English and Chinese<->Japanese work immediately after install. The
speech recognition model and a compact translation model for those pairs are
bundled inside the app (912 MiB total).

WHAT NEEDS AN OPTIONAL DOWNLOAD
Other language pairs (English<->Japanese, Korean, Cantonese) use a larger
general-purpose translation model. It is deliberately NOT bundled, because
including it would push the download from 912 MiB to about 1.95 GiB.

HOW TO TEST THE OTHER PAIRS
1. Tap the gear icon to open Settings.
2. Find the "Model packages" section — it names exactly which package is
   missing.
3. Import a .vtmodel file via the Files app or AirDrop.
   If you need a ready-to-import package, contact us at <your-email> and we
   will send a download link.

The app never crashes or hangs when a model is missing: it always shows a
clear message naming the missing package and offering a "Go to Settings"
button.

WHAT TO LOOK AT
- Tap the large microphone button and speak; transcription and translation
  appear line by line, fully offline.
- Tap the speaker icon on any translated line to hear it read aloud.
- The app requests microphone access only. Nothing is uploaded, nothing is
  written to disk.
```

---

## 2. App Store 元数据草稿

| 字段 | 限制 | 草稿 |
| --- | --- | --- |
| App 名称 | ≤ 30 字符 | `多国语音互译` |
| 副标题 | ≤ 30 字符 | `离线中日英韩粤 语音翻译`（12 字） |
| 主分类 | — | 工具 / Utilities |
| 次分类 | — | 效率 / Productivity |
| 关键词 | ≤ 100 字符 | `离线,翻译,语音,录音,转写,中日,中英,粤语,韩语,同传,会议,旅行,私密` |
| 支持 URL | 必填 | `<https://your-site/support>` |
| 隐私政策 URL | 必填 | `<https://your-site/privacy>` |
| 年龄分级 | — | 4+（无用户生成内容、无广告、无内购） |

**描述草稿：**

```
完全离线的语音翻译 —— 录音不出设备，译文不用等网络。

【开箱即用】
中文↔英文、中文↔日文无需任何额外下载，装好就能用。

【其他语向按需导入】
英文↔日文、韩语、粤语等语向使用更大的通用翻译模型。为把安装包控制在
合理体积，这些模型由你在设置里按需导入，不需要就永远不占空间。

【为什么是离线的】
录音只在本机内存里处理，不上传、不保存到磁盘。没有账号、没有登录、
没有网络请求 —— 飞行模式下功能完全不受影响。

【连续翻译】
自动按停顿断句，边说边出结果，适合面对面交流。
译文可逐句朗读，也可一键分享为文本。
```

---

## 3. 隐私政策草案

App Store Connect 必填。这个 App 反而是最好写的 —— 直接托管成静态页即可。

```
多国语音互译 · 隐私政策

生效日期：<日期>

一、我们不收集任何数据
本应用没有账号体系，不与任何服务器通信，不包含广告、分析或任何第三方
统计 SDK。开发者无法获取你使用本应用的任何信息。

二、录音如何处理
语音识别与翻译全部在本机完成。录音数据仅存在于内存中，用于当次识别，
处理完毕后即被丢弃 —— 它不会被写入磁盘，也不会离开你的设备。

三、麦克风权限
本应用仅在你主动点击录音按钮时请求麦克风权限。你可以随时在系统
「设置 → 隐私 → 麦克风」中撤销授权；撤销后仅录音功能不可用，其余功能
不受影响。

四、模型包
你可以自行导入翻译模型文件（.vtmodel）。这些文件仅保存在本应用的
沙盒目录中，不会被上传。

五、儿童隐私
本应用不收集任何数据，因此不涉及儿童个人信息的收集。

六、政策变更
如本政策有变更，我们会在应用更新说明中告知。

七、联系方式
<your-email>
```

---

## 4. 截图清单

`TARGETED_DEVICE_FAMILY = "1,2"` → iPhone 与 iPad **都要**出图。

| 设备 | 尺寸要求 | 建议张数 | 拍什么 |
| --- | --- | --- | --- |
| iPhone | 6.7"（1290×2796） | 3–5 张 | ① 翻译主界面有内容 ② 朗读播放中 ③ 设置页模型包区块 ④ 分享面板 |
| iPad | 13"（2064×2752） | 3–5 张 | 同上；顺带证明 iPad 布局正常（审核会看） |

建议在**真机**上截：目标机 iPad mini 4 是 7.9"，不是 13"，截图需另找设备或在模拟器里跑 iPad 13"。

---

## 5. 提交前检查清单

- [ ] `ios/PrivacyInfo.xcprivacy` 存在，且 `ls <X.app>/PrivacyInfo.xcprivacy` 能验到
- [ ] `scripts/gen-ios-project.py` 已包含隐私清单引用（否则下次重跑生成脚本会静默丢失）
- [ ] `ios/Info.plist` 有 `ITSAppUsesNonExemptEncryption = false`
- [ ] 版本号 `1.0.0` / `1`，且 `ios/VoiceTranslator.xcodeproj/project.pbxproj` 已同步
- [ ] 确认 `DEVELOPMENT_TEAM = 8E63E2MJB9` 是**付费**开发者账号
- [ ] `<your-email>` 已在审核备注与隐私政策里替换成本人邮箱
- [ ] 隐私政策页已上线，URL 可访问
- [ ] 两套截图已备齐
- [ ] TestFlight 内测跑过一轮：麦克风授权、连续录音、朗读、缺包提示
- [ ] 可执行文件 `__TEXT` 段合计 < 500 MB（`size -m <X.app>/VoiceTranslator`）
- [ ] iPad 分屏 / 旋转 / 动态字号真机验证过
- [ ] 若上中国大陆区：ICP 备案已办妥（App Store Connect 合规信息页复核当前口径）
- [ ] `scripts/tests/` 回归全绿
