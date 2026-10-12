#!/usr/bin/env python3
"""列出 `models/*.vtmodel` 的档位、体检「能不能被 App 导入」，并把内置的那几份打进两端。

## 内置集合（v0.2.10 起）

| kind | 内置？ | 为什么 |
| --- | --- | --- |
| `asr` 转写 | ✅ 内置（229 MiB） | 没有它整个 App 都跑不起来，必须开箱可用 |
| `opus` 专用翻译 | ✅ 内置（548 MiB） | 体积可控，且它决定「智能选择」档能不能用上 |
| `translation` 通用翻译 | ❌ **不内置**（1.04 GiB） | 三份里最大，内置会让两端安装包翻倍 → 由用户在设置页导入 |

🔴 **这个「谁内置谁不内置」写死在顶部 `BUNDLED` 里，不是「取体积小的那些」。**
`scripts/tests/test_no_bundled_models.py` 会断言 `BUNDLED ⊆ RECOMMENDED`、
`translation ∉ BUNDLED`，以及两端产物里确实只有这两份 —— 单靠体积策略会静默漂。

两端形态不同（**同一份源包，两种落法**）：

| 端 | 落在哪 | 形态 | 为什么 |
| --- | --- | --- | --- |
| iOS | `ios/BundledModels/<kind>/` | **解包后的只读目录** | bundle 里的目录可直接随机读，不必复制到用户空间 |
| Android | `app/src/main/assets/bundled-models/<kind>.vtmodel` | **原样的 STORED zip** | assets 只能流式读 → 首启解到内部存储再用 |

外加 `ios/BundledModels/index.json`：App **从它动态得知这一版内置了哪几类**，
不写死在 Swift 里（换内置集合不用改 App 代码）。

## 体检的三条判据（别处都不验）

| 判据 | 不满足会怎样 |
| --- | --- |
| `manifest.json` 是**第一个** zip entry | Android 走顺序读器（`VtStoredZip`）解包，第一个 entry 必须是清单；否则报「缺少有效 manifest.json，请选择 .vtmodel 模型包」—— 用户会以为文件下错了 |
| 带 ZIP64 记录（包尾有 `PK\\x06\\x07`） | 中央目录偏移越过 2 GiB 后 Android `ZipFile` 报 `invalid CEN header (bad signature)`。⚠️ **包小于 2 GiB 也可能踩到**：差的是「一个中央目录的长度」，不是文件大小 |
| 清单里有 `licenses/` 文件 | 两端在解包前就拒绝（「缺少许可文件」） |

## 推荐档位为什么**钉住**，而不是取体积最小

⚠️ 顶部那张 `RECOMMENDED` 刻意不是「每个 kind 取最小」。踩过一次：
translation 的推荐档是 **Q4_K_M**（A8 的 armv8-a 基线口径下速度第一、质量优于 Q4_0；
Q4_0 还会在中译粤时稳定复读崩坏、中译韩混英文实词），可 `models/` 里 **Q3 更小** ——
任何隐式的「取最小」策略都会把推荐悄悄漂到 Q3，而输出是「成功」，看日志发现不了。

所以把「刻意选定的档位」写死成表，并且**把依据打出来**。表里没有的 kind 就只说
「没有推荐」，不替它挑一个 —— 静默挑一个正是上面那个坑的形态。

## 用法

    python3 scripts/model-tiers.py                          # 列档位 + 体检
    python3 scripts/model-tiers.py --emit                   # 写两端的内部置产物
    python3 scripts/model-tiers.py --json dist/models/catalog.json
    python3 scripts/model-tiers.py --strict                 # 任何一个候选坏掉就退出 1

退出码：0 = 没问题；1 = 推荐/内置档位体检不通过（`--strict` 下任意候选不通过）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

from model_pack import zip64_info

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

# 三档模型包。与 iOS `ModelPackages.Kind`、Android `ModelPackages.KINDS`、
# scripts/model_pack.py 的 ENGINES 四处必须对齐。
KINDS = ["asr", "translation", "opus"]

# 推荐档位。换档位请改这张表，并在旁边写清依据（别让下一个人以为是算出来的）。
RECOMMENDED = {
    "asr": "sensevoice-four-language-int8-v1",
    "translation": "qwen25-1.5b-q4km-zh-ja-ko-en-yue-v1",
    "opus": "opus-mt-zh-en-ja-v1",
}

# 🔴 **打进 App 的那几份**（v0.2.10）。与 `RECOMMENDED` 是两张**不同**的表：
# RECOMMENDED 说「用户在设置页该导哪一份」，BUNDLED 说「哪几份不用导」。
# 两者的关系是不变量：BUNDLED ⊆ RECOMMENDED，且 NEVER_BUNDLED ∩ BUNDLED = ∅。
BUNDLED = {
    "asr": "sensevoice-four-language-int8-v1",
    "opus": "opus-mt-zh-en-ja-v1",
}

# 刻意**永不内置**的 kind。写出来是为了让「不内置」是一条被断言的规则，
# 而不是「恰好没配」—— 两者的区别在下一个人想加内置时才会体现出来。
NEVER_BUNDLED = ("translation",)

IOS_BUNDLED_DIR = ROOT / "ios" / "BundledModels"
ANDROID_BUNDLED_DIR = ROOT / "app" / "src" / "main" / "assets" / "bundled-models"

KIND_NOTE = {
    "asr": "语音转写（sherpa-onnx SenseVoice）。**必需**，没有它无法开始翻译。",
    "translation": "通用翻译模型（llama.cpp）。**必需**，所有语向的兜底。",
    "opus": "专用翻译引擎（OPUS-MT）。**可选**，只覆盖它能翻的语向；没有则设置页的「智能选择」降级成「统一模型」。",
}

MANIFEST = "manifest.json"


def die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def human(size: int) -> str:
    return f"{size / (1 << 30):.2f} GiB" if size >= 1 << 30 else f"{size / 1048576:.0f} MiB"


class Candidate:
    """一个候选模型包。`first_entry` 由 `scan()` 填 —— 默认值只服务于测试注入。"""

    def __init__(self, path: Path, manifest: dict, first_entry: str = MANIFEST) -> None:
        self.path = path
        self.manifest = manifest
        self.kind = manifest["kind"]
        self.id = manifest["id"]
        self.name = manifest.get("name", manifest["id"])
        self.engine = manifest["engine"]
        self.total = sum(f["size"] for f in manifest["files"])
        self.first_entry = first_entry

    def bytes_on_disk(self) -> int:
        """用户要下载/拷贝的是整个包，不是清单里文件之和（差一个 zip 头尾）。

        ⚠️ 包不在磁盘上时退回清单声明之和，**不抛** —— 这只是个显示用的数字，
        真正的「文件不在」由 `health()` 报成人话。直接 stat 会让一个显示问题
        伪装成脚本崩溃（上一版在这里踩过。）
        """
        try:
            return self.path.stat().st_size
        except OSError:
            return self.total

    def declared_license(self) -> bool:
        return any(f["path"].startswith("licenses/") for f in self.manifest["files"])


def scan() -> dict[str, list[Candidate]]:
    """扫描 `models/`，按 kind 归类。坏包只警告、不中断（其余包仍可体检）。"""
    if not MODELS_DIR.is_dir():
        die(f"找不到模型包目录：{MODELS_DIR}")

    found: dict[str, list[Candidate]] = {}
    for path in sorted(MODELS_DIR.glob("*.vtmodel")):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
            candidate = Candidate(path, manifest, names[0] if names else "")
        except Exception as exc:  # 一个坏包不该让整条流水线停摆
            print(f"  跳过 {path.name}：{exc}", file=sys.stderr)
            continue
        if candidate.kind not in KINDS:
            print(f"  跳过 {path.name}：未知 kind={candidate.kind}", file=sys.stderr)
            continue
        found.setdefault(candidate.kind, []).append(candidate)
    return found


def health(candidate: Candidate) -> list[str]:
    """体检一个包，返回问题清单（空 = 没问题）。

    ⚠️ 这些判据**只在打包侧验一次**：运行期两端都会照单全收（iOS 甚至不做 SHA 复校），
    所以包一旦坏了，症状会出现在用户设备上而不是这里。
    """
    problems: list[str] = []

    if candidate.first_entry != MANIFEST:
        problems.append(
            f"manifest.json 不是第一个 zip entry（实际是 {candidate.first_entry!r}）："
            "Android 的顺序读器会报「缺少有效 manifest.json」，用户会以为下错了文件")

    if not candidate.declared_license():
        problems.append("清单里没有 licenses/ 文件：两端都会在解包前拒绝（「缺少许可文件」）")

    # ⚠️ 先判存在性再 stat —— 候选不一定来自真实扫盘（测试会注入假候选），
    # 直接 stat 会抛 FileNotFoundError，把「文件不在磁盘上」伪装成脚本崩溃。
    # 这是从上一版 bundle-models.py 学来的：那道闸踩过一次。
    if not candidate.path.is_file():
        problems.append("包不在磁盘上，无法体检 ZIP64")
        return problems

    locator, _eocd = zip64_info(candidate.path)
    if locator < 0:
        problems.append(
            "缺 ZIP64 记录（包尾没有 PK\\x06\\x07）：中央目录偏移越过 2 GiB 后 "
            "Android 报 invalid CEN header。修法见 scripts/model_pack.py repack")

    return problems


def emit(found: dict[str, list[Candidate]], report: dict[str, list[str]]) -> None:
    """把 `BUNDLED` 里点名的包写进两端。

    ⚠️ 这个函数是**唯一**往 App 目录写东西的地方；`model-tiers.py` 其余部分只读。

    两端的形态刻意不同（见模块 docstring），但**源包是同一份**：
    iOS 解包成只读目录，Android 原样拷 `.vtmodel`。
    """
    # —— 不变量：谁内置是规则，不是巧合 ——
    unknown = set(BUNDLED) - set(RECOMMENDED)
    if unknown:
        die(f"BUNDLED 里的 {sorted(unknown)} 不在 RECOMMENDED 里 —— "
            "内置的必须也是推荐档位，否则「内置哪一档」会在两处各说一套")
    clash = set(BUNDLED) & set(NEVER_BUNDLED)
    if clash:
        die(f"BUNDLED 里出现了 {sorted(clash)}，但它在 NEVER_BUNDLED 里。"
            "如果确实要改内置集合，请**同时**改这两张表，并说清体积代价 —— "
            "translation 是 1.04 GiB，内置会让两端安装包翻倍")

    picks: dict[str, Candidate] = {}
    for kind, package_id in BUNDLED.items():
        match = [c for c in found.get(kind, []) if c.id == package_id]
        if not match:
            die(f"内置档位 {kind}={package_id} 不在 models/ 下；"
                "先跑 scripts/prepare_models.py / pack-opus-models.py 把它准备好")
        candidate = match[0]
        if report[candidate.id]:
            die(f"内置档位 {candidate.id} 体检不通过，不能打进 App：\n  "
                + "\n  ".join(report[candidate.id]))
        picks[kind] = candidate

    # —— iOS：解包成只读目录 + index.json ——
    # 先整个重来，避免上一轮留下的 kind 目录被当成「这一版还内置了它」。
    if IOS_BUNDLED_DIR.exists():
        shutil.rmtree(IOS_BUNDLED_DIR)
    IOS_BUNDLED_DIR.mkdir(parents=True)
    index = []
    for kind in KINDS:                      # 按固定顺序，输出可比对
        candidate = picks.get(kind)
        if candidate is None:
            continue
        target = IOS_BUNDLED_DIR / kind
        target.mkdir(parents=True)
        _extract(candidate, target)
        index.append({"kind": kind, "id": candidate.id, "name": candidate.name})
    # ⚠️ 顶层是**数组**：iOS `ModelPackages.bundledIndex` 就是这么解它的。
    # 新增字段可以，别改成对象 —— 那会让 App 静默读不到索引、以为「没有内置包」。
    (IOS_BUNDLED_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # —— Android：原样拷 .vtmodel（assets 只能流式读，首启解包） ——
    # 同样先清空：**这里就是「translation 不许出现在 assets 里」的落点**，
    # 只按 BUNDLED 重建，旧的 translation.vtmodel 自然不会被留下。
    if ANDROID_BUNDLED_DIR.exists():
        shutil.rmtree(ANDROID_BUNDLED_DIR)
    ANDROID_BUNDLED_DIR.mkdir(parents=True)
    for kind in KINDS:
        candidate = picks.get(kind)
        if candidate is None:
            continue
        shutil.copy2(candidate.path, ANDROID_BUNDLED_DIR / f"{kind}.vtmodel")

    # —— 汇总 ——
    print("已写入内置模型包：")
    total = 0
    for kind in KINDS:
        candidate = picks.get(kind)
        if candidate is None:
            continue
        size = candidate.bytes_on_disk()
        total += size
        print(f"  {kind:<12} {human(size):>9}  {candidate.id}")
        print(f"               iOS      {IOS_BUNDLED_DIR.relative_to(ROOT)}/{kind}/")
        print(f"               Android  {(ANDROID_BUNDLED_DIR / (kind + '.vtmodel')).relative_to(ROOT)}")
    print(f"  {'合计':<12} {human(total):>9}")
    skipped = [k for k in NEVER_BUNDLED if k in found]
    if skipped:
        print(f"  不内置     {', '.join(skipped)}：由用户在设置页导入")
    print()
    print("⚠️ iOS 侧改完必须重跑 scripts/gen-ios-project.py（工程引用的是整个 BundledModels 目录）。")
    print()


def _extract(candidate: Candidate, target: Path) -> None:
    """把 `.vtmodel` 解到 target，并逐文件复校大小与 SHA-256。

    体检只证明**容器**没问题（清单在首位、有 ZIP64、有 licenses），
    这里证明**解出来的字节**与清单一致 —— 两者都要，缺一个就会在用户设备上才炸。
    """
    with zipfile.ZipFile(candidate.path) as zf:
        for spec in candidate.manifest["files"]:
            name = spec["path"]
            if not _safe(name):
                die(f"{candidate.path.name} 里有危险路径：{name!r}")
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            digest = hashlib.sha256()
            with destination.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            if destination.stat().st_size != spec["size"]:
                die(f"{candidate.path.name}:{name} 解出来的大小与清单不符")
            if digest.hexdigest() != spec["sha256"]:
                die(f"{candidate.path.name}:{name} 解出来的 SHA-256 与清单不符")
        # 清单本身也要落盘：两端都靠它认这个目录（iOS `bundled()` 就是查它）。
        (target / MANIFEST).write_bytes(zf.read(MANIFEST))


def _safe(name: str) -> bool:
    """与两端 `ModelPackages.safe()` 同一条规则。"""
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def verify_emitted() -> int:
    """体检**已经写进仓库**的内置产物（不碰 `models/`，所以没有源包也能跑）。

    与 `--emit` 是配对的两半：`--emit` 负责**写对**，这里负责**写完没被人改坏**。
    `scripts/build-ios.sh` 每次构建都会调它 —— 因为这四种坏法里有三种
    **不会让 xcodebuild 报错**，只在真机上表现为「某个功能莫名其妙不可用」：

    | 坏法 | 为什么编译器不报 |
    | --- | --- |
    | 目录存在但工程没引用 | xcodebuild 照编，只是**静默不打包** |
    | 缺 `index.json` | App 读到空索引，`isBundled()` 全 false → 三行全「未就绪」 |
    | 多内置了一份（如 translation） | 一切正常，只是安装包大 1 GB |
    | 两端集合不一致 | 各自都对，只是同一版本两个行为 |
    """
    problems: list[str] = []
    expected = dict(BUNDLED)

    # —— 1) iOS 索引必须与 BUNDLED 一致 ——
    index_path = IOS_BUNDLED_DIR / "index.json"
    actual_index: dict[str, str] = {}
    if not IOS_BUNDLED_DIR.is_dir():
        problems.append(f"{IOS_BUNDLED_DIR.relative_to(ROOT)} 不存在 —— "
                        "生成它：python3 scripts/model-tiers.py --emit")
    elif not index_path.is_file():
        problems.append(f"{index_path.relative_to(ROOT)} 不存在 —— App 靠它得知内置了哪几类；"
                        "缺了它会以为「没有内置包」，三行全显示未就绪")
    else:
        try:
            actual_index = {e["kind"]: e["id"] for e in
                            json.loads(index_path.read_text(encoding="utf-8"))}
        except Exception as exc:
            problems.append(f"{index_path.relative_to(ROOT)} 读不出来：{exc}")
    if actual_index and actual_index != expected:
        problems.append("ios/BundledModels/index.json 与 BUNDLED 不一致："
                        f"索引 {sorted(actual_index)}，表里 {sorted(expected)}")

    for kind in expected:
        if not (IOS_BUNDLED_DIR / kind / MANIFEST).is_file():
            problems.append(f"ios/BundledModels/{kind}/manifest.json 不存在")
    if IOS_BUNDLED_DIR.is_dir():
        for child in sorted(IOS_BUNDLED_DIR.iterdir()):
            if child.is_dir() and child.name not in expected:
                problems.append(f"ios/BundledModels/{child.name}/ 不在 BUNDLED 里（多内置了一份）")

    # —— 2) Android assets 必须与 BUNDLED 一一对应 ——
    if not ANDROID_BUNDLED_DIR.is_dir():
        problems.append(f"{ANDROID_BUNDLED_DIR.relative_to(ROOT)} 不存在 —— "
                        "生成它：python3 scripts/model-tiers.py --emit")
    else:
        actual = sorted(p.stem for p in ANDROID_BUNDLED_DIR.glob("*.vtmodel"))
        if actual != sorted(expected):
            problems.append("app/src/main/assets/bundled-models/ 与 BUNDLED 不一致："
                            f"实际 {actual}，应为 {sorted(expected)}")

    # —— 3) NEVER_BUNDLED 一个都不许出现（本轮需求的核心） ——
    for kind in NEVER_BUNDLED:
        if (IOS_BUNDLED_DIR / kind).is_dir():
            problems.append(f"ios/BundledModels/{kind}/ 存在，但 {kind} 在 NEVER_BUNDLED 里")
        if (ANDROID_BUNDLED_DIR / f"{kind}.vtmodel").is_file():
            problems.append(f"app/src/main/assets/bundled-models/{kind}.vtmodel 存在，"
                            f"但 {kind} 在 NEVER_BUNDLED 里")

    # —— 4) 工程文件必须引用它 ——
    pbx = ROOT / "ios" / "VoiceTranslator.xcodeproj" / "project.pbxproj"
    if not pbx.is_file():
        problems.append("ios/VoiceTranslator.xcodeproj/project.pbxproj 不存在")
    elif IOS_BUNDLED_DIR.is_dir() and "BundledModels" not in pbx.read_text(encoding="utf-8"):
        problems.append("Xcode 工程里没有引用 BundledModels —— 它不会进 bundle，"
                        "而 xcodebuild 不会报错。修法：python3 scripts/gen-ios-project.py")

    if problems:
        print("内置产物检查不通过：", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 1

    print("内置产物检查通过：")
    for kind in KINDS:
        if kind in expected:
            print(f"  {kind:<12} {expected[kind]}")
    for kind in NEVER_BUNDLED:
        print(f"  {kind:<12} 不内置（由用户在设置页导入）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="列出 models/ 下的模型包档位，体检能否被 App 导入，并按需打进两端")
    parser.add_argument("--emit", action="store_true",
                        help="把 BUNDLED 里点名的包写进 ios/BundledModels 与 app assets")
    parser.add_argument("--verify", action="store_true",
                        help="只检查已写进仓库的内置产物是否与 BUNDLED 一致（不需要 models/）")
    parser.add_argument("--json", type=Path, default=None, metavar="PATH",
                        help="把档位目录写成 JSON（供发布说明 / 分发页用）")
    parser.add_argument("--strict", action="store_true",
                        help="任意一个候选体检不通过就退出 1（默认只对推荐/内置档位硬失败）")
    args = parser.parse_args()

    # `--verify` 刻意**不**扫 models/：它要能在只有仓库、没有源包的机器上跑
    # （CI / 别人的 checkout 都会是这种情况）。
    if args.verify:
        return verify_emitted()

    found = scan()
    if not found:
        die("models/ 下没有可用的 .vtmodel —— 先跑 scripts/prepare_models.py "
            "或 scripts/prepare_translategemma.py，或 scripts/pack-opus-models.py")

    # 体检全部候选（包括非推荐的：用户照样可能导入它们）。
    report: dict[str, list[str]] = {c.id: health(c) for cs in found.values() for c in cs}

    bundled_ids = set(BUNDLED.values())

    print()
    print("模型包档位（标记「内置」的随 App 分发，其余由用户在设置页导入）")
    print()

    catalog: dict = {"schemaVersion": 1, "packages": {}}
    hard_failures: list[Candidate] = []

    for kind in KINDS:
        candidates = sorted(found.get(kind, []), key=lambda c: c.bytes_on_disk())
        print(f"{kind}：{KIND_NOTE[kind]}")
        if not candidates:
            print("  ⚠️ models/ 下没有此类包"
                  + ("（没有它就无法开始翻译）" if kind != "opus" else "（不影响使用）"))
            print()
            continue

        pinned = RECOMMENDED.get(kind)
        if pinned and not any(c.id == pinned for c in candidates):
            # 不替它挑一个 —— 静默挑一个正是「推荐漂档」那个坑的形态。
            print(f"  ⚠️ 推荐档位 {pinned} 不在 models/ 下；以下都不是推荐档位，"
                  "要么补齐它，要么改本脚本顶部的 RECOMMENDED 并写明依据",
                  file=sys.stderr)

        for candidate in candidates:
            recommended = candidate.id == pinned
            is_bundled = candidate.id in bundled_ids
            if is_bundled:
                mark = "→ 内置"
            elif recommended:
                mark = "→ 推荐"
            else:
                mark = "  可选"
            print(f"  {mark}  {human(candidate.bytes_on_disk()):>9}  {candidate.path.name}")
            print(f"              id={candidate.id}  engine={candidate.engine}")
            for problem in report[candidate.id]:
                print(f"              ✗ {problem}")
            # 内置的比推荐的更硬：打进包里的坏东西会让**所有人**开箱就坏。
            if report[candidate.id] and (is_bundled or recommended):
                hard_failures.append(candidate)
            catalog["packages"][candidate.id] = {
                "kind": kind,
                "id": candidate.id,
                "name": candidate.name,
                "engine": candidate.engine,
                "fileName": candidate.path.name,
                "bytes": candidate.bytes_on_disk(),
                "required": kind != "opus",
                "recommended": recommended,
                "bundled": is_bundled,
                "languages": candidate.manifest.get("languages", []),
                "problems": report[candidate.id],
            }

        print()

    # 汇总：用户最少要导入哪些、合计多大。内置的那几份不用导。
    to_import = [c for kind in ("asr", "translation", "opus")
                 for c in found.get(kind, [])
                 if c.id == RECOMMENDED.get(kind) and c.id not in bundled_ids]
    bundled_named = [c for kind in ("asr", "translation", "opus")
                     for c in found.get(kind, []) if c.id in bundled_ids]
    print("模型包清单：")
    for c in bundled_named:
        print(f"  {c.kind:<12} {c.path.name:<40} {human(c.bytes_on_disk()):>9}  随 App 分发")
    for c in to_import:
        note = "必需" if c.kind != "opus" else "可选"
        print(f"  {c.kind:<12} {c.path.name:<40} {human(c.bytes_on_disk()):>9}  需导入（{note}）")
    need = sum(c.bytes_on_disk() for c in to_import if c.kind != "opus")
    extra = sum(c.bytes_on_disk() for c in to_import if c.kind == "opus")
    tail = f"（必需 {human(need)}" + (f" + 可选 {human(extra)}）" if extra else "）")
    print(f"  {'需用户导入合计':<12} {'':<40} {human(need + extra):>9}  {tail}")
    print()
    print("（每个 kind 的推荐档位由本脚本顶部的 RECOMMENDED 显式钉住，"
          "**不是**取体积最小；内置集合由顶部的 BUNDLED 显式钉住，**不是**取体积最小。）")
    print()

    bad = [c for cs in found.values() for c in cs if report[c.id]]
    if bad and not hard_failures:
        print(f"提示：{len(bad)} 个非推荐包体检不通过（上面带 ✗ 的那些）；"
              "它们仍可被导入，但风险自负。", file=sys.stderr)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print(f"档位目录已写入 {args.json}")

    if hard_failures:
        die("推荐/内置档位体检不通过：" + "、".join(c.id for c in hard_failures))
    if args.strict and bad:
        die(f"--strict：{len(bad)} 个候选包体检不通过")

    if args.emit:
        emit(found, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
