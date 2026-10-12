#!/usr/bin/env python3
"""生成 ios/VoiceTranslator.xcodeproj/project.pbxproj。

为什么用脚本生成而不是把 pbxproj 当手写文件维护：
- 源文件按目录分组，增删文件后重跑本脚本即可，不用手改 UUID；
- UUID 由「文件路径 + 用途」的 md5 派生，同一个文件每次都得到同一个 ID，
  于是 git diff 只在真正增删文件时才变化（否则每次打开 Xcode 都会全文件重排）。

用法：
    python3 scripts/gen-ios-project.py            # 生成/更新工程
    python3 scripts/gen-ios-project.py --check    # 只校验，不写盘（CI 用）

生成物：
    ios/VoiceTranslator.xcodeproj/project.pbxproj

设计约束（对应 iOS 移植详细设计书）：
- 部署目标 iOS 15.0（设计书 §1）；
- Swift 语言模式 5（不是 6）：三线程 Pipeline 大量使用跨线程回调，
  在 Swift 6 严格并发下会变成硬错误，首期先保证可编译可跑，见 README 的说明；
- ObjC++ 桥的 .mm 必须能被编译，但**不要求** sherpa-onnx / llama.cpp 已就位：
  它们用 `#if __has_include(...)` 守卫，缺库时退化成 isAvailable = NO。
- `native/nmt/` 下两端共享的 C++ 源码（OPUS-MT 推理核心）必须进 Sources 阶段 ——
  见 SHARED_NATIVE。这些文件在 ios/ 之外，rglob 扫不到，要显式收录。
"""

from __future__ import annotations

import argparse
import hashlib
import plistlib
import sys
from pathlib import Path

IOS_DIR = Path(__file__).resolve().parent.parent / "ios"
SRC_DIR_NAME = "VoiceTranslator"
PROJECT_NAME = "VoiceTranslator"
BUNDLE_ID = "com.shiefzhang.voicetranslator"
DEPLOYMENT_TARGET = "15.0"
SWIFT_VERSION = "5.0"

# 版本号以 ios/Info.plist 为唯一数据源：那两个键在 Info.plist 里写的是**字面量**
# （不是 $(MARKETING_VERSION)），运行时生效的是它们。这里读出来回填 pbxproj，
# 免得两处各写一份、日后对不上 —— 之前就发生过 pbxproj 停留在 0.2.3 的情况。
def _info_plist_version() -> tuple[str, str]:
    try:
        with (IOS_DIR / "Info.plist").open("rb") as handle:
            plist = plistlib.load(handle)
    except Exception as exc:
        raise SystemExit(f"读不到 ios/Info.plist 的版本号：{exc}")
    return (str(plist.get("CFBundleShortVersionString", "0.0")),
            str(plist.get("CFBundleVersion", "0")))


MARKETING_VERSION, CURRENT_PROJECT_VERSION = _info_plist_version()

# 目录展示顺序（未列出的目录按字母序排在后面）
GROUP_ORDER = ["App", "Core", "Engines", "Models", "Views", "Bridge"]

# 两端共享的 C++ 源码 —— 在 ios/VoiceTranslator/ 之外，但必须进 Sources 阶段。
#
# `native/nmt/` 是 OPUS-MT(Marian) 的推理核心：一份实现两端共用，
# Android 侧由 app/src/main/cpp/CMakeLists.txt 引同一批文件，iOS 侧必须也编进来
# —— 否则 VTOpusNmt.mm 里的 vt_opus_load / vt_opus_translate 全是未定义符号。
#
# 刻意**显式列文件**而不是扫目录：native/nmt/ 下还躺着 vt_opus_probe.cc，
# 那是 macOS 上的验收探针，带 main()，编进来会跟 App 的 main 直接冲突。
#
# 表结构：{相对 ios/VoiceTranslator 的目录: [该目录下要编的文件名]}。
# 用相对路径而不是绝对路径，换台机器重跑本脚本才能生成同一份工程（UUID 由路径派生，
# 一旦掺进绝对路径，uuid 会随 checkout 位置漂移，git diff 就永远不干净）。
SHARED_NATIVE = {
    "../../native/nmt": ["vt_opus.cc"],
    "../../native/nmt/third_party/simple-sentencepiece/ssentencepiece/csrc":
        ["ssentencepiece.cc"],
}

# 这批文件必须按 **C++17** 编，而工程其余部分是 gnu++20（见 shared_project 里
# 的 CLANG_CXX_LANGUAGE_STANDARD）。原因在 vendor 进来的 simple-sentencepiece：
#
#   threadpool.h:45  ->  std::future<typename std::result_of<F(Args...)>::type>
#
# `std::result_of` 在 **C++20 里已被移除**，libc++ 里直接找不到，于是报
# `no type named 'result_of' in namespace 'std'`，并连带把整个头文件解析带崩 ——
# 后面会跟着冒出几十条「unknown type name 'path'」「use of undeclared identifier」
# 「incomplete type」之类的**假错误**（实测 2026-09-28：同一份文件 gnu++20 报
# 20+ 条错，gnu++17 零错）。排查时别被那一大串带偏，只看第一条。
#
# 为什么不干脆把工程降到 gnu++17：iOS 侧只有这几个 .cc 是 C++，其余是 Swift / ObjC++，
# 降标准对它们没意义，却会悄悄改掉 ObjC++ 那边的语义。用 per-file 的
# COMPILER_FLAGS 精确打在 PBXBuildFile 上，也与 Android 侧一致
# （app/src/main/cpp/CMakeLists.txt 是 CMAKE_CXX_STANDARD 17）。
SHARED_NATIVE_FLAGS = "-std=gnu++17"

SWIFT_EXTS = {".swift"}
SOURCES_EXTS = {".swift", ".mm"}
SOURCE_EXTS = {".swift", ".mm", ".m", ".c", ".cpp"}


def uid(*parts: str) -> str:
    """由字符串派生 24 位十六进制 UUID（稳定、可复现）。"""
    digest = hashlib.md5("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:24].upper()


def q(value: str) -> str:
    """pbxproj 字符串：含特殊字符时加引号。"""
    if value == "":
        return '""'
    if all(c.isalnum() or c in "._/$" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Project:
    def __init__(self) -> None:
        self.build_files: list[str] = []
        self.file_refs: list[str] = []
        self.groups: list[str] = []
        self.sections: dict[str, list[str]] = {}
        self.used: set[str] = set()

    def add(self, section: str, ident: str, body: list[str], comment: str = "") -> str:
        if ident in self.used:
            raise SystemExit(f"UUID 冲突：{ident}（{section}）")
        self.used.add(ident)
        self.sections.setdefault(section, [])
        head = f"\t\t{ident}"
        if comment:
            head += f" /* {comment} */"
        head += " = {"
        self.sections[section].append("\n".join([head, *body, "\t\t};"]))
        return ident

    def render(self, root_object: str) -> str:
        out = ["// !$*UTF8*$!", "{", "\tarchiveVersion = 1;", "\tclasses = {", "\t};",
               "\tobjectVersion = 56;", "\tobjects = {", ""]
        for section, entries in self.sections.items():
            out.append(f"/* Begin {section} section */")
            out.extend(entries)
            out.append(f"/* End {section} section */")
            out.append("")
        out.append("\t};")
        out.append(f"\trootObject = {root_object} /* Project object */;")
        out.append("}")
        return "\n".join(out) + "\n"


def collect_files(ios_dir: Path) -> dict[str, list[Path]]:
    """返回 {相对目录: [文件]}，相对目录 '' 表示工程根。"""
    src = ios_dir / SRC_DIR_NAME
    if not src.is_dir():
        raise SystemExit(f"找不到源码目录：{src}")

    tree: dict[str, list[Path]] = {}
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name == ".DS_Store":
            continue
        if path.suffix not in SOURCE_EXTS and path.suffix not in {".h"}:
            continue
        rel = path.relative_to(src).parent
        tree.setdefault(str(rel) if str(rel) != "." else "", []).append(path)
    for files in tree.values():
        files.sort(key=lambda p: p.name)
    return tree


def sort_group_keys(keys: list[str]) -> list[str]:
    def key(name: str):
        try:
            return (0, GROUP_ORDER.index(name))
        except ValueError:
            return (1, name)

    return sorted(keys, key=key)


def build(ios_dir: Path) -> str:
    project = Project()
    tree = collect_files(ios_dir)

    proj_uuid = uid("project")
    target_uuid = uid("target", PROJECT_NAME)
    product_uuid = uid("product", PROJECT_NAME)
    root_group = uid("group", "<root>")
    products_group = uid("group", "<products>")
    sources_phase = uid("phase", "sources")
    frameworks_phase = uid("phase", "frameworks")
    resources_phase = uid("phase", "resources")
    target_cfg_list = uid("cfglist", "target")
    project_cfg_list = uid("cfglist", "project")
    info_plist_uuid = uid("fileref", "Info.plist")
    base_xcconfig_uuid = uid("fileref", "Config/Base.xcconfig")

    # Asset catalog（App 图标等）存在才进工程；图标本体在 Assets.xcassets/AppIcon.appiconset/
    has_assets = (ios_dir / "Assets.xcassets").is_dir()
    if has_assets:
        assets_uuid = uid("fileref", "Assets.xcassets")
        assets_build_uuid = uid("buildfile", "Assets.xcassets")

    # ---------- 内置模型包（v0.2.10：只有 asr 与 opus） ----------
    #
    # ⚠️ 必须是 **folder reference**（`lastKnownFileType = folder`），不能是 group：
    # group 会把目录**压平**成 bundle 根，于是 asr/manifest.json 与 opus/manifest.json
    # 撞名、后一个静默覆盖前一个。判据：`find X.app/BundledModels -name manifest.json`
    # 应该每个 kind 各有一份；只剩一份就是被压平了。
    #
    # 内容与「内置哪几类」由 `scripts/model-tiers.py --emit` 决定（BUNDLED 表），
    # 这里只负责把已存在的目录引用进工程。目录不存在时不引用（工程仍能生成）——
    # 但 `scripts/build-ios.sh` 会硬拦「该有却没有」的情况，见那边的双向检查。
    has_bundled = (ios_dir / "BundledModels").is_dir()
    if has_bundled:
        bundled_uuid = uid("fileref", "BundledModels")
        bundled_build_uuid = uid("buildfile", "BundledModels")
        if not (ios_dir / "BundledModels" / "index.json").is_file():
            raise SystemExit(
                "ios/BundledModels/ 存在但没有 index.json —— 先跑 "
                "`python3 scripts/model-tiers.py --emit` 重建，别手工拼目录")

    # ---------- 隐私清单（App Store 上架必需） ----------
    #
    # ⚠️ 必须落在 **bundle 根目录**（与 Info.plist 同级），Apple 才认。
    # 自 2024-05-01 起，用到 required reason API 却不在清单里声明理由的构建，
    # App Store Connect **直接拒收** —— 不是警告，是上传失败。本 App 命中的
    # 只有 UserDefaults（理由 CA92.1，见 ios/PrivacyInfo.xcprivacy 的注解）。
    #
    # 为什么要在生成器里显式引用：本工程是**整体重写** pbxproj 的，手拖进 Xcode
    # 的文件会在下次重跑本脚本时静默消失，而构建照常成功 —— 症状是「本地打包
    # 一切正常、上传报缺隐私清单」，极难定位。判据是**产物**里有没有：
    #   ls <X.app>/PrivacyInfo.xcprivacy
    #
    # 文件不存在时不引用（工程仍能生成），但 build-ios.sh 会在归档路径上拦这一条。
    has_privacy = (ios_dir / "PrivacyInfo.xcprivacy").is_file()
    if has_privacy:
        privacy_uuid = uid("fileref", "PrivacyInfo.xcprivacy")
        privacy_build_uuid = uid("buildfile", "PrivacyInfo.xcprivacy")

    # ---------- PBXFileReference ----------
    project.add(
        "PBXFileReference", base_xcconfig_uuid,
        ["\t\t\tisa = PBXFileReference;", "\t\t\tlastKnownFileType = text.xcconfig;",
         "\t\t\tpath = Config/Base.xcconfig;", "\t\t\tsourceTree = \"<group>\";"],
        "Base.xcconfig",
    )

    project.add(
        "PBXFileReference", info_plist_uuid,
        ["\t\t\tisa = PBXFileReference;", "\t\t\tlastKnownFileType = text.plist.xml;",
         "\t\t\tpath = Info.plist;", "\t\t\tsourceTree = \"<group>\";"],
        "Info.plist",
    )

    if has_assets:
        project.add(
            "PBXFileReference", assets_uuid,
            ["\t\t\tisa = PBXFileReference;", "\t\t\tlastKnownFileType = folder.assetcatalog;",
             "\t\t\tpath = Assets.xcassets;", "\t\t\tsourceTree = \"<group>\";"],
            "Assets.xcassets",
        )
        project.add(
            "PBXBuildFile", assets_build_uuid,
            ["\t\t\tisa = PBXBuildFile;", f"\t\t\tfileRef = {assets_uuid} /* Assets.xcassets */;"],
            "Assets.xcassets in Resources",
        )

    if has_bundled:
        # ⚠️ `lastKnownFileType = folder`，别写成 folder.assetcatalog（那会走 Asset
        # Catalog 编译器处理，整个目录被当成一个资源包）。
        project.add(
            "PBXFileReference", bundled_uuid,
            ["\t\t\tisa = PBXFileReference;", "\t\t\tlastKnownFileType = folder;",
             "\t\t\tpath = BundledModels;", "\t\t\tsourceTree = \"<group>\";"],
            "BundledModels",
        )
        project.add(
            "PBXBuildFile", bundled_build_uuid,
            ["\t\t\tisa = PBXBuildFile;", f"\t\t\tfileRef = {bundled_uuid} /* BundledModels */;"],
            "BundledModels in Resources",
        )

    if has_privacy:
        # `text.plist.xml`：.xcprivacy 本体就是 XML plist。它进 Resources 阶段后
        # 被**原样拷贝**到 bundle 根（不会被当 Info.plist 特殊处理 —— 只有名字
        # 叫 Info.plist 的才走那套）。
        project.add(
            "PBXFileReference", privacy_uuid,
            ["\t\t\tisa = PBXFileReference;", "\t\t\tlastKnownFileType = text.plist.xml;",
             "\t\t\tpath = PrivacyInfo.xcprivacy;", "\t\t\tsourceTree = \"<group>\";"],
            "PrivacyInfo.xcprivacy",
        )
        project.add(
            "PBXBuildFile", privacy_build_uuid,
            ["\t\t\tisa = PBXBuildFile;", f"\t\t\tfileRef = {privacy_uuid} /* PrivacyInfo.xcprivacy */;"],
            "PrivacyInfo.xcprivacy in Resources",
        )

    project.add(
        "PBXFileReference", product_uuid,
        ["\t\t\tisa = PBXFileReference;", "\t\t\texplicitFileType = wrapper.application;",
         "\t\t\tincludeInIndex = 0;", f"\t\t\tpath = {PROJECT_NAME}.app;",
         "\t\t\tsourceTree = BUILT_PRODUCTS_DIR;"],
        f"{PROJECT_NAME}.app",
    )

    src_refs: dict[str, str] = {}
    for rel_dir, files in tree.items():
        for path in files:
            rel = path.relative_to(ios_dir).as_posix()
            ref = uid("fileref", rel)
            ext = path.suffix
            file_type = {
                ".swift": "sourcecode.swift",
                ".mm": "sourcecode.cpp.objcpp",
                ".h": "sourcecode.c.h",
            }[ext]
            project.add(
                "PBXFileReference", ref,
                ["\t\t\tisa = PBXFileReference;", f"\t\t\tlastKnownFileType = {file_type};",
                 f"\t\t\tpath = {q(path.name)};", "\t\t\tsourceTree = \"<group>\";"],
                path.name,
            )
            src_refs[rel] = ref

    # ---------- PBXBuildFile（只有可编译的进 Sources 阶段）----------
    build_refs: dict[str, str] = {}
    for rel_dir, files in tree.items():
        for path in files:
            if path.suffix not in SOURCES_EXTS:
                continue
            rel = path.relative_to(ios_dir).as_posix()
            ref = src_refs[rel]
            bf = uid("buildfile", rel)
            project.add(
                "PBXBuildFile", bf,
                [f"\t\t\tisa = PBXBuildFile;", f"\t\t\tfileRef = {ref} /* {path.name} */;"],
                f"{path.name} in Sources",
            )
            build_refs[rel] = bf

    # ---------- 两端共享的原生源码（见 SHARED_NATIVE）----------
    #
    # 这些文件在 ios/VoiceTranslator/ 之外，所以不走 collect_files 那套。
    # 每个目录建一个 group（带 path），于是文件引用只用裸文件名，
    # 不用把 `../../` 塞进 PBXFileReference.path —— 后者虽然也能解析，
    # 但 Xcode 导航器里会显示成一长串相对路径，而且到处是踩坑的余地。
    shared_refs: dict[str, str] = {}
    shared_groups: list[str] = []
    for group_rel in sorted(SHARED_NATIVE):
        children = []
        for name in sorted(SHARED_NATIVE[group_rel]):
            # uid 的输入刻意用 `VoiceTranslator/<相对目录>/<文件名>` 形式：
            # 与普通源码的 `fileref` 键（相对 ios/ 的 posix 路径）风格一致，
            # 又天然不会跟 ios/ 下的同路径撞车。
            key_str = f"VoiceTranslator/{group_rel}/{name}"
            ref = uid("fileref", key_str)
            project.add(
                "PBXFileReference", ref,
                ["\t\t\tisa = PBXFileReference;",
                 "\t\t\tlastKnownFileType = sourcecode.cpp.cpp;",
                 f"\t\t\tpath = {q(name)};", "\t\t\tsourceTree = \"<group>\";"],
                name,
            )
            bf = uid("buildfile", key_str)
            project.add(
                "PBXBuildFile", bf,
                ["\t\t\tisa = PBXBuildFile;",
                 f"\t\t\tfileRef = {ref} /* {name} */;",
                 # per-file C++ 标准，见 SHARED_NATIVE_FLAGS 的说明。
                 f"\t\t\tsettings = {{COMPILER_FLAGS = \"{SHARED_NATIVE_FLAGS}\";}};"],
                f"{name} in Sources",
            )
            shared_refs[key_str] = bf
            children.append(ref)
        gid = uid("group", "shared:" + group_rel)
        shared_groups.append(gid)
        project.add(
            "PBXGroup", gid,
            ["\t\t\tisa = PBXGroup;", "\t\t\tchildren = ("]
            + [f"\t\t\t\t{c}," for c in children]
            + ["\t\t\t);", f"\t\t\tpath = {q(group_rel)};", "\t\t\tsourceTree = \"<group>\";"],
            Path(group_rel).name,
        )

    # ---------- PBXGroup ----------
    def group_body(children: list[str]) -> list[str]:
        body = ["\t\t\tisa = PBXGroup;", "\t\t\tchildren = ("]
        body += [f"\t\t\t\t{child}," for child in children]
        body += ["\t\t\t);", "\t\t\tsourceTree = \"<group>\";"]
        return body

    def key(path: Path) -> str:
        """所有 ref/buildfile 的统一键：相对 ios/ 的 posix 路径。"""
        return path.relative_to(ios_dir).as_posix()

    # 顶层子目录 group（Bridge 等），再往下不再建子目录（当前层级只有一层）
    dir_group_ids: dict[str, str] = {}
    for rel_dir in sort_group_keys(list(tree.keys())):
        if rel_dir == "":
            continue
        children = [src_refs[key(p)] for p in tree[rel_dir]]
        gid = uid("group", rel_dir)
        dir_group_ids[rel_dir] = gid
        project.add(
            "PBXGroup", gid,
            ["\t\t\tisa = PBXGroup;", "\t\t\tchildren = ("]
            + [f"\t\t\t\t{c}," for c in children]
            + ["\t\t\t);", f"\t\t\tpath = {q(rel_dir)};", "\t\t\tsourceTree = \"<group>\";"],
            rel_dir,
        )

    src_group_children = [dir_group_ids[d] for d in sort_group_keys(list(dir_group_ids))]
    src_group_children += [src_refs[key(p)] for p in tree.get("", [])]
    src_group_children += shared_groups
    src_group = uid("group", SRC_DIR_NAME)
    project.add(
        "PBXGroup", src_group,
        group_body(src_group_children)[:-2]
        + ["\t\t\t);", f"\t\t\tpath = {q(SRC_DIR_NAME)};", "\t\t\tsourceTree = \"<group>\";"],
        SRC_DIR_NAME,
    )

    project.add(
        "PBXGroup", products_group,
        group_body([f"{product_uuid} /* {PROJECT_NAME}.app */"]),
        "Products",
    )
    project.add(
        "PBXGroup", root_group,
        group_body([f"{src_group} /* {SRC_DIR_NAME} */",
                    f"{base_xcconfig_uuid} /* Base.xcconfig */",
                    f"{info_plist_uuid} /* Info.plist */",
                    *([f"{privacy_uuid} /* PrivacyInfo.xcprivacy */"] if has_privacy else []),
                    *([f"{assets_uuid} /* Assets.xcassets */"] if has_assets else []),
                    *([f"{bundled_uuid} /* BundledModels */"] if has_bundled else []),
                    f"{products_group} /* Products */"]),
        "",
    )

    # ---------- Build phases ----------
    # Sources 阶段 = 普通源码（按 rel 排序）+ 共享原生源码（SHARED_NATIVE）。
    sources_entries = [f"\t\t\t\t{build_refs[rel]} /* {Path(rel).name} in Sources */,"
                       for rel in sorted(build_refs)]
    sources_entries += [f"\t\t\t\t{shared_refs[k]} /* {Path(k).name} in Sources */,"
                        for k in sorted(shared_refs)]
    project.add(
        "PBXSourcesBuildPhase", sources_phase,
        ["\t\t\tisa = PBXSourcesBuildPhase;", "\t\t\tbuildActionMask = 2147483647;", "\t\t\tfiles = ("]
        + sources_entries
        + ["\t\t\t);", "\t\t\trunOnlyForDeploymentPostprocessing = 0;"],
        "Sources",
    )
    project.add(
        "PBXFrameworksBuildPhase", frameworks_phase,
        ["\t\t\tisa = PBXFrameworksBuildPhase;", "\t\t\tbuildActionMask = 2147483647;",
         "\t\t\tfiles = (", "\t\t\t);", "\t\t\trunOnlyForDeploymentPostprocessing = 0;"],
        "Frameworks",
    )
    resource_entries = []
    if has_privacy:
        resource_entries.append(f"\t\t\t\t{privacy_build_uuid} /* PrivacyInfo.xcprivacy in Resources */,")
    if has_assets:
        resource_entries.append(f"\t\t\t\t{assets_build_uuid} /* Assets.xcassets in Resources */,")
    if has_bundled:
        resource_entries.append(f"\t\t\t\t{bundled_build_uuid} /* BundledModels in Resources */,")
    project.add(
        "PBXResourcesBuildPhase", resources_phase,
        ["\t\t\tisa = PBXResourcesBuildPhase;", "\t\t\tbuildActionMask = 2147483647;",
         "\t\t\tfiles = (", *resource_entries, "\t\t\t);", "\t\t\trunOnlyForDeploymentPostprocessing = 0;"],
        "Resources",
    )

    # ---------- Target ----------
    project.add(
        "PBXNativeTarget", target_uuid,
        [
            "\t\t\tisa = PBXNativeTarget;",
            f"\t\t\tbuildConfigurationList = {target_cfg_list} /* Build configuration list for PBXNativeTarget \"{PROJECT_NAME}\" */;",
            "\t\t\tbuildPhases = (",
            f"\t\t\t\t{sources_phase} /* Sources */,",
            f"\t\t\t\t{frameworks_phase} /* Frameworks */,",
            f"\t\t\t\t{resources_phase} /* Resources */,",
            "\t\t\t);",
            "\t\t\tbuildRules = (",
            "\t\t\t);",
            "\t\t\tdependencies = (",
            "\t\t\t);",
            f"\t\t\tname = {PROJECT_NAME};",
            f"\t\t\tproductName = {PROJECT_NAME};",
            f"\t\t\tproductReference = {product_uuid} /* {PROJECT_NAME}.app */;",
            "\t\t\tproductType = \"com.apple.product-type.application\";",
        ],
        PROJECT_NAME,
    )

    # ---------- Project ----------
    project.add(
        "PBXProject", proj_uuid,
        [
            "\t\t\tisa = PBXProject;",
            "\t\t\tattributes = {",
            "\t\t\t\tBuildIndependentTargetsInParallel = 1;",
            "\t\t\t\tLastSwiftUpdateCheck = 2700;",
            "\t\t\t\tLastUpgradeCheck = 2700;",
            "\t\t\t\tTargetAttributes = {",
            f"\t\t\t\t\t{target_uuid} = {{",
            "\t\t\t\t\t\tCreatedOnToolsVersion = 27.0;",
            "\t\t\t\t\t};",
            "\t\t\t\t};",
            "\t\t\t};",
            f"\t\t\tbuildConfigurationList = {project_cfg_list} /* Build configuration list for PBXProject \"{PROJECT_NAME}\" */;",
            "\t\t\tcompatibilityVersion = \"Xcode 14.0\";",
            "\t\t\tdevelopmentRegion = en;",
            "\t\t\thasScannedForEncodings = 0;",
            "\t\t\tknownRegions = (",
            "\t\t\t\ten,",
            "\t\t\t\tBase,",
            "\t\t\t\t\"zh-Hans\",",
            "\t\t\t);",
            f"\t\t\tmainGroup = {root_group};",
            f"\t\t\tproductRefGroup = {products_group} /* Products */;",
            "\t\t\tprojectDirPath = \"\";",
            "\t\t\tprojectRoot = \"\";",
            "\t\t\ttargets = (",
            f"\t\t\t\t{target_uuid} /* {PROJECT_NAME} */,",
            "\t\t\t);",
        ],
        "Project object",
    )

    # ---------- 构建设置 ----------
    shared_project = [
        "ALWAYS_SEARCH_USER_PATHS = NO;",
        "CLANG_ANALYZER_NONNULL = YES;",
        "CLANG_ANALYZER_NUMBER_OBJECT_CONVERSION = YES_AGGRESSIVE;",
        "CLANG_CXX_LANGUAGE_STANDARD = \"gnu++20\";",
        "CLANG_ENABLE_MODULES = YES;",
        "CLANG_ENABLE_OBJC_ARC = YES;",
        "CLANG_ENABLE_OBJC_WEAK = YES;",
        "CLANG_WARN_BLOCK_CAPTURE_AUTORELEASING = YES;",
        "CLANG_WARN_BOOL_CONVERSION = YES;",
        "CLANG_WARN_COMMA = YES;",
        "CLANG_WARN_CONSTANT_CONVERSION = YES;",
        "CLANG_WARN_DIRECT_OBJC_ISA_USAGE = YES_ERROR;",
        "CLANG_WARN_DOCUMENTATION_COMMENTS = YES;",
        "CLANG_WARN_EMPTY_BODY = YES;",
        "CLANG_WARN_ENUM_CONVERSION = YES;",
        "CLANG_WARN_INFINITE_RECURSION = YES;",
        "CLANG_WARN_INT_CONVERSION = YES;",
        "CLANG_WARN_NON_LITERAL_NULL_CONVERSION = YES;",
        "CLANG_WARN_OBJC_IMPLICIT_RETAIN_SELF = YES;",
        "CLANG_WARN_OBJC_LITERAL_CONVERSION = YES;",
        "CLANG_WARN_OBJC_ROOT_CLASS = YES_ERROR;",
        "CLANG_WARN_QUOTED_INCLUDE_IN_FRAMEWORK_HEADER = YES;",
        "CLANG_WARN_RANGE_LOOP_ANALYSIS = YES;",
        "CLANG_WARN_STRICT_PROTOTYPES = YES;",
        "CLANG_WARN_SUSPICIOUS_MOVE = YES;",
        "CLANG_WARN_UNGUARDED_AVAILABILITY = YES_AGGRESSIVE;",
        "CLANG_WARN_UNREACHABLE_CODE = YES;",
        "CLANG_WARN__DUPLICATE_METHOD_MATCH = YES;",
        "COPY_PHASE_STRIP = NO;",
        "ENABLE_STRICT_OBJC_MSGSEND = YES;",
        "ENABLE_USER_SCRIPT_SANDBOXING = YES;",
        "GCC_C_LANGUAGE_STANDARD = gnu17;",
        "GCC_NO_COMMON_BLOCKS = YES;",
        "GCC_WARN_64_TO_32_BIT_CONVERSION = YES;",
        "GCC_WARN_ABOUT_RETURN_TYPE = YES_ERROR;",
        "GCC_WARN_UNDECLARED_SELECTOR = YES;",
        "GCC_WARN_UNINITIALIZED_AUTOS = YES_AGGRESSIVE;",
        "GCC_WARN_UNUSED_FUNCTION = YES;",
        "GCC_WARN_UNUSED_VARIABLE = YES;",
        f"IPHONEOS_DEPLOYMENT_TARGET = {DEPLOYMENT_TARGET};",
        "MTL_FAST_MATH = YES;",
        "SDKROOT = iphoneos;",
        "SWIFT_EMIT_LOC_STRINGS = YES;",
        f"SWIFT_VERSION = {SWIFT_VERSION};",
        "TARGETED_DEVICE_FAMILY = \"1,2\";",
    ]

    def cfg(name: str, extra: list[str], base_ref: str | None = None) -> str:
        body = ["\t\t\tisa = XCBuildConfiguration;"]
        if base_ref:
            body.append(f"\t\t\tbaseConfigurationReference = {base_ref} /* Base.xcconfig */;")
        body.append("\t\t\tbuildSettings = {")
        body += [f"\t\t\t\t{line}" for line in [*shared_project, *extra]]
        body += ["\t\t\t};", f"\t\t\tname = {name};"]
        return project.add("XCBuildConfiguration", uid("cfg", "project", name), body, name)

    proj_debug = cfg("Debug", [
        "DEBUG_INFORMATION_FORMAT = dwarf;",
        "ENABLE_TESTABILITY = YES;",
        "GCC_DYNAMIC_NO_PIC = NO;",
        "GCC_OPTIMIZATION_LEVEL = 0;",
        "GCC_PREPROCESSOR_DEFINITIONS = (",
        "\t\"DEBUG=1\",",
        "\t\"$(inherited)\",",
        ");",
        "MTL_ENABLE_DEBUG_INFO = INCLUDE_SOURCE;",
        "ONLY_ACTIVE_ARCH = YES;",
        "SWIFT_ACTIVE_COMPILATION_CONDITIONS = \"DEBUG $(inherited)\";",
        "SWIFT_OPTIMIZATION_LEVEL = \"-Onone\";",
    ], base_ref=base_xcconfig_uuid)

    proj_release = cfg("Release", [
        "DEBUG_INFORMATION_FORMAT = \"dwarf-with-dsym\";",
        "ENABLE_NS_ASSERTIONS = NO;",
        "GCC_OPTIMIZATION_LEVEL = s;",
        "MTL_ENABLE_DEBUG_INFO = NO;",
        "SWIFT_COMPILATION_MODE = wholemodule;",
        "SWIFT_OPTIMIZATION_LEVEL = \"-O\";",
        "VALIDATE_PRODUCT = YES;",
    ], base_ref=base_xcconfig_uuid)

    target_common = [
        "CODE_SIGN_STYLE = Automatic;",
        f"CURRENT_PROJECT_VERSION = {CURRENT_PROJECT_VERSION};",
        # Xcode 16 起 Debug 默认把主代码编成 VoiceTranslator.debug.dylib + 一个
        # __preview.dylib 垫片。本项目在链接这个 dylib 时会失败：
        #   ld: cannot link directly with 'SwiftUICore' because product
        #       being built is not an allowed client of it
        # （SwiftUICore 是 SwiftUI 的 SPI 框架，不允许被 dylib 直接链接。）
        # 关掉它就回到经典的单可执行文件链接方式，代价只是增量 Debug 稍慢。
        "ENABLE_DEBUG_DYLIB = NO;",
        "GENERATE_INFOPLIST_FILE = NO;",
        "INFOPLIST_FILE = Info.plist;",
        "LD_RUNPATH_SEARCH_PATHS = (",
        "\t\"$(inherited)\",",
        "\t\"@executable_path/Frameworks\",",
        ");",
        f"MARKETING_VERSION = {MARKETING_VERSION};",
        f"PRODUCT_BUNDLE_IDENTIFIER = {BUNDLE_ID};",
        "PRODUCT_NAME = \"$(TARGET_NAME)\";",
        "SWIFT_OBJC_BRIDGING_HEADER = \"VoiceTranslator/Bridge/VoiceTranslator-Bridging-Header.h\";",
    ]

    def tcfg(name: str, extra: list[str]) -> str:
        body = ["\t\t\tisa = XCBuildConfiguration;", "\t\t\tbuildSettings = {"]
        body += [f"\t\t\t\t{line}" for line in [*target_common, *extra]]
        body += ["\t\t\t};", f"\t\t\tname = {name};"]
        return project.add("XCBuildConfiguration", uid("cfg", "target", name), body, name)

    tcfg("Debug", [])
    tcfg("Release", [])

    project.add(
        "XCConfigurationList", project_cfg_list,
        ["\t\t\tisa = XCConfigurationList;", "\t\t\tbuildConfigurations = (",
         f"\t\t\t\t{proj_debug} /* Debug */,", f"\t\t\t\t{proj_release} /* Release */,",
         "\t\t\t);", "\t\t\tdefaultConfigurationIsVisible = 0;",
         "\t\t\tdefaultConfigurationName = Release;"],
        f"Build configuration list for PBXProject \"{PROJECT_NAME}\"",
    )
    project.add(
        "XCConfigurationList", target_cfg_list,
        ["\t\t\tisa = XCConfigurationList;", "\t\t\tbuildConfigurations = (",
         f"\t\t\t\t{uid('cfg', 'target', 'Debug')} /* Debug */,",
         f"\t\t\t\t{uid('cfg', 'target', 'Release')} /* Release */,",
         "\t\t\t);", "\t\t\tdefaultConfigurationIsVisible = 0;",
         "\t\t\tdefaultConfigurationName = Release;"],
        f"Build configuration list for PBXNativeTarget \"{PROJECT_NAME}\"",
    )

    return project.render(proj_uuid)


def _post_action_script_text() -> str:
    """scheme Post-action 的命令行（已做 XML 属性转义）。

    Xcode 27 的 Post-action 环境里**没有任何工程路径变量**：
    SRCROOT / PROJECT_DIR / BUILD_DIR 全部为空 —— 连 <EnvironmentBuildable>
    都补不回来（实测 2026-09-21，xcodebuild 与 IDE 一致）。探测到的可靠锚点
    只有 $HOME，所以这里把脚本路径按 $HOME 相对化**直接写死**进 scheme。
    scheme 本来就是生成物：换机器重跑 gen-ios-project.py 会按新位置重算。

    顺带说明：Xcode 27 的 xcodebuild 在**构建成功后也会执行 Post-action**
    （与旧版行为不同）。install-ipad.sh 内部调 xcodebuild 时会置
    VT_INSTALL_IPAD_ACTIVE=1，Post-action 再调回本脚本时靠它防重入。
    """
    repo = IOS_DIR.parent
    home = Path.home()
    try:
        root = "$HOME/" + repo.relative_to(home).as_posix()
    except ValueError:
        root = str(repo)
    cmd = f'"{root}/scripts/install-ipad.sh" --if-connected --no-build'
    return (
        cmd.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


SCHEME_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Scheme
   LastUpgradeVersion = "2700"
   version = "1.7">
   <BuildAction
      parallelizeBuildables = "YES"
      buildImplicitDependencies = "YES">
      <BuildActionEntries>
         <BuildActionEntry
            buildForTesting = "YES"
            buildForRunning = "YES"
            buildForProfiling = "YES"
            buildForArchiving = "YES"
            buildForAnalyzing = "YES">
            <BuildableReference
               BuildableIdentifier = "primary"
               BlueprintIdentifier = "{target_uuid}"
               BuildableName = "{product}.app"
               BlueprintName = "{product}"
               ReferencedContainer = "container:{project}.xcodeproj">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
      <PostActions>
         <ExecutionAction
            ActionType = "Xcode.IDEStandardExecutionActionsCore.ExecutionActionType.ShellScriptAction">
            <ActionContent
               title = "Install to connected iOS device"
               scriptText = "{post_action}&#10;"
               shellToInvoke = "/bin/bash">
            </ActionContent>
         </ExecutionAction>
      </PostActions>
   </BuildAction>
   <TestAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      shouldUseLaunchSchemeArgsEnv = "YES">
      <Testables>
      </Testables>
   </TestAction>
   <LaunchAction
      buildConfiguration = "Debug"
      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"
      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"
      launchStyle = "0"
      useCustomWorkingDirectory = "NO"
      ignoresPersistentStateOnLaunch = "NO"
      debugDocumentVersioning = "YES"
      debugServiceExtension = "internal"
      allowLocationSimulation = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{target_uuid}"
            BuildableName = "{product}.app"
            BlueprintName = "{product}"
            ReferencedContainer = "container:{project}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction
      buildConfiguration = "Release"
      shouldUseLaunchSchemeArgsEnv = "YES"
      savedToolIdentifier = ""
      useCustomWorkingDirectory = "NO"
      debugDocumentVersioning = "YES">
      <BuildableProductRunnable
         runnableDebuggingMode = "0">
         <BuildableReference
            BuildableIdentifier = "primary"
            BlueprintIdentifier = "{target_uuid}"
            BuildableName = "{product}.app"
            BlueprintName = "{product}"
            ReferencedContainer = "container:{project}.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction
      buildConfiguration = "Debug">
   </AnalyzeAction>
   <ArchiveAction
      buildConfiguration = "Release"
      revealArchiveInOrganizer = "YES">
   </ArchiveAction>
</Scheme>
"""


def write_scheme(out_dir: Path) -> None:
    scheme_dir = out_dir / "xcshareddata" / "xcschemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / f"{PROJECT_NAME}.xcscheme").write_text(
        SCHEME_TEMPLATE.format(
            target_uuid=uid("target", PROJECT_NAME),
            product=PROJECT_NAME,
            project=PROJECT_NAME,
            post_action=_post_action_script_text(),
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验是否与磁盘一致")
    args = parser.parse_args()

    content = build(IOS_DIR)
    out_dir = IOS_DIR / f"{PROJECT_NAME}.xcodeproj"
    out_file = out_dir / "project.pbxproj"

    if args.check:
        if not out_file.is_file():
            print(f"缺少 {out_file}", file=sys.stderr)
            return 1
        if out_file.read_text(encoding="utf-8") != content:
            print("project.pbxproj 与源码树不一致，请运行 scripts/gen-ios-project.py", file=sys.stderr)
            return 1
        print("project.pbxproj 已是最新")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(content, encoding="utf-8")

    workspace_dir = out_dir / "project.xcworkspace"
    if not workspace_dir.is_dir():
        workspace_dir.mkdir(parents=True)
    (workspace_dir / "contents.xcworkspacedata").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Workspace version = \"1.0\">\n"
        f"   <FileRef location = \"self:{PROJECT_NAME}.xcodeproj\">\n"
        "   </FileRef>\n"
        "</Workspace>\n",
        encoding="utf-8",
    )
    write_scheme(out_dir)
    print(f"已生成 {out_file.relative_to(IOS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
