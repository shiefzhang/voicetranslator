#!/usr/bin/env python3
"""隐私清单体检：`ios/PrivacyInfo.xcprivacy` 在不在、**会不会真的进 bundle**。

## 为什么需要这个脚本

自 2024-05-01 起，用到 required reason API 却不在 `PrivacyInfo.xcprivacy` 里声明
理由的构建，App Store Connect **直接拒收** —— 不是警告，是上传失败：

    https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api

本 App 命中的只有 `UserDefaults`（`Models/ModelPackages.swift` 用它记引擎档位、
朗读模式、导入的包名与路径），对应理由 `CA92.1`。

## 为什么必须在**构建期**拦，而不是等上传

这个坏法的三个变体，**本地全都没症状**：

| 坏法 | 本地表现 | 上传表现 |
| --- | --- | --- |
| 文件被删 | 编译、安装、运行全正常 | 拒收 |
| 文件在、工程没引用 | 同上 | 拒收 |
| 只改 Xcode / 手改 pbxproj | 同上（本地能跑） | 拒收 —— 直到下次重跑生成脚本才「好」，然后再次拒收 |

「文件在」和「工程引用了」是两件事：只有列进 Resources 阶段 `files = (…)` 的
条目才会被拷进 `.app`。所以四条判据缺一不可。

## 还有一条专门针对本仓库

`ios/VoiceTranslator.xcodeproj/project.pbxproj` 是 `scripts/gen-ios-project.py`
**整体重写**生成的。只在 Xcode 里（或手改 pbxproj）加引用，下次重跑生成脚本就
静默消失 —— 所以「生成器里也得有引用」单独算一条判据。

⚠️ 判生成器那一跳时按**非注释行**判：生成器的注释里就写着 `PrivacyInfo.xcprivacy`
这几个字（就在解释为什么要改它），对全文判等于让注释替代码过审。

## 用法

    python3 scripts/verify-privacy-manifest.py          # 体检，0 = 通过

退出码：0 = 四条判据全过；1 = 有判据不过（原因打到 stderr）。
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "ios" / "PrivacyInfo.xcprivacy"
PBXPROJ = ROOT / "ios" / "VoiceTranslator.xcodeproj" / "project.pbxproj"
GENERATOR = ROOT / "scripts" / "gen-ios-project.py"

# required reason API 的分类与理由。本 App 只命中 UserDefaults：
#   CA92.1 = 仅访问本 App 自身的 user defaults（不含跨 App / 广告用途）。
EXPECTED_API_REASONS = {
    "NSPrivacyAccessedAPICategoryUserDefaults": "CA92.1",
}


def resources_phase(pbxproj_text: str) -> str:
    """切出 PBXResourcesBuildPhase 那一段。

    全局 `assertIn` 会假绿：`PrivacyInfo.xcprivacy` 这个名字在工程里出现四处
    （文件引用对象、构建文件对象、分组 children、Resources 阶段），
    删掉**真正决定打包的那一条**，按全文判照样通过，而产物里啥都没有。
    """
    try:
        head, rest = pbxproj_text.split("/* Begin PBXResourcesBuildPhase section */", 1)
    except ValueError:
        raise SystemExit("工程文件里找不到 PBXResourcesBuildPhase 段")
    return rest.split("/* End PBXResourcesBuildPhase section */", 1)[0]


def check() -> list[str]:
    """返回问题清单，空列表 = 全部通过。"""
    problems: list[str] = []

    # ---- ① 文件存在且是合法 plist ----
    if not MANIFEST.is_file():
        problems.append(
            f"缺 {MANIFEST.relative_to(ROOT)} —— App Store Connect 会拒收上传")
        return problems

    try:
        with MANIFEST.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要把任何解析失败都报出来
        problems.append(f"{MANIFEST.name} 不是合法的 plist：{exc}")
        return problems

    # ---- ② 四项声明与实际行为一致 ----
    if data.get("NSPrivacyTracking") is not False:
        problems.append(
            "NSPrivacyTracking 必须是 false —— 本 App 零网络、零分析 SDK、不跨 App 跟踪")
    if data.get("NSPrivacyCollectedDataTypes") != []:
        problems.append("NSPrivacyCollectedDataTypes 必须为空数组 —— 本 App 不收集任何数据")
    if data.get("NSPrivacyTrackingDomains") != []:
        problems.append("NSPrivacyTrackingDomains 必须为空数组")

    declared = {entry.get("NSPrivacyAccessedAPIType"):
                entry.get("NSPrivacyAccessedAPITypeReasons")
                for entry in data.get("NSPrivacyAccessedAPITypes", [])}
    for api, reason in EXPECTED_API_REASONS.items():
        if api not in declared:
            problems.append(f"没声明 {api} —— 代码里用了它（见 Models/ModelPackages.swift）")
        elif reason not in (declared[api] or []):
            problems.append(f"{api} 的理由必须是 {reason}，现在是 {declared[api]}")

    # ---- ③ 工程 Resources 阶段引用了它 ----
    if PBXPROJ.is_file():
        phase = resources_phase(PBXPROJ.read_text(encoding="utf-8"))
        entries = [line for line in phase.splitlines() if line.strip().endswith(",")]
        if not any("PrivacyInfo.xcprivacy" in line for line in entries):
            problems.append(
                "工程的 Resources 阶段没有 PrivacyInfo.xcprivacy —— 文件在、但不会进 "
                ".app，上传照样被拒。修法：python3 scripts/gen-ios-project.py")
    else:
        print("  · 跳过工程检查：还没生成 Xcode 工程（干净克隆上正常）")

    # ---- ④ 生成器里也有引用（本仓库专有，见模块 docstring） ----
    code_lines = [line for line in GENERATOR.read_text(encoding="utf-8").splitlines()
                  if not line.lstrip().startswith("#")]
    if not any("buildfile" in line and "PrivacyInfo.xcprivacy" in line for line in code_lines):
        problems.append(
            "gen-ios-project.py 没有为隐私清单建 PBXBuildFile —— 重跑生成脚本后引用会"
            "静默消失（注释里提到不算）")
    if not any("resource_entries.append" in line and "privacy" in line for line in code_lines):
        problems.append(
            "gen-ios-project.py 建了引用，但没接进 Resources 阶段")

    return problems


def main() -> int:
    problems = check()
    if problems:
        print("隐私清单判据未通过：", file=sys.stderr)
        for item in problems:
            print(f"  ✗ {item}", file=sys.stderr)
        return 1
    print("  ✓ 隐私清单：文件、四项声明、工程 Resources 引用、生成器引用 —— 全过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
