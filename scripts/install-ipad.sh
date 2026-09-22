#!/usr/bin/env bash
#
# install-ipad.sh —— 构建 VoiceTranslator 并安装到已连接的 iOS 真机。
#
# == 为什么需要这个脚本 ==
#
# Xcode 27 把「真机调试 / 部署」的最低系统门槛抬到了 iOS 17：
#     https://developer.apple.com/support/xcode/
#     Xcode 27 · Device Support: iOS 17 or later
#   （对比 Xcode 26.6 还是 iOS 15 or later）
#
# iPad mini 4（iPad5,1 / J96AP）最高只能升到 iPadOS 15.8.x，因此：
#   - 它 **不会** 出现在 Xcode 的设备列表里；
#   - `xcrun devicectl` 把它标成 `pairingState: unsupported`（CoreDevice 新协议直接拒绝）；
#   - 在 Xcode 27 里按 ⌘R 直接装，这条路是死的 —— 没有任何工程设置能改。
#
# 但设备本身完全正常：macOS 的 usbmuxd / lockdownd **老协议栈**照常工作，
# `ios-deploy` 走的正是老协议，能识别设备、能安装、能取日志。
#
# 于是把这条件链拆成两段：
#     Xcode       负责「编译 + 签名」  （Xcode 27 做得很好，deployment target 15.0 完全合法）
#     ios-deploy  负责「安装到设备」
#
# 本脚本就是把这两段串起来。装好后 App 图标出现在 iPad 上，
# 之后直接在 iPad 上点开即可 —— ios-deploy 没有独立的 launch 动作。
#
# == 依赖 ==
#   brew install ios-deploy
#
# == 用法 ==
#   scripts/install-ipad.sh                  # Debug，自动选第一台 USB 设备
#   scripts/install-ipad.sh --release        # Release
#   scripts/install-ipad.sh --udid <UDID>    # 指定设备（40 位十六进制）
#   scripts/install-ipad.sh --no-build       # 跳过构建，直接装上次的产物
#   scripts/install-ipad.sh --list           # 只列出当前连接的设备
#
set -euo pipefail

# —— 防重入 ——
# Xcode 27 的 xcodebuild 在构建成功后会执行 scheme 的 Post-action，
# 而 Post-action 调的就是本脚本（--no-build）。本脚本自己调 xcodebuild 时
# 会置 VT_INSTALL_IPAD_ACTIVE=1，那次嵌套调用走到这里直接退出，
# 否则：完整构建 → Post-action 再装一遍（慢一倍），构建失败时还会互相干扰。
if [ "${VT_INSTALL_IPAD_ACTIVE:-}" = "1" ]; then
  echo "（Post-action 重入：本脚本已在安装流程中，跳过）"
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/ios"
BUNDLE_ID="com.shiefzhang.voicetranslator"

CONFIG=Debug
UDID=""
DO_BUILD=1
LIST_ONLY=0
IF_CONNECTED=0          # 1 = 没插设备时静默退出（供 Xcode scheme Post-action 使用）
APP_PATH_OVERRIDE=""    # 显式指定的 .app 路径

while [ $# -gt 0 ]; do
  case "$1" in
    --release)      CONFIG=Release ;;
    --debug)        CONFIG=Debug ;;
    --udid)         UDID="${2:?--udid 需要一个 40 位十六进制 UDID}"; shift ;;
    --no-build)     DO_BUILD=0 ;;
    --list)         LIST_ONLY=1 ;;
    --app-path)     APP_PATH_OVERRIDE="${2:?--app-path 需要 .app 的路径}"; shift ;;
    --if-connected) IF_CONNECTED=1 ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

# —— PATH 加固 ——
# Xcode Post-action / 从 GUI 启动的进程 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
# 没有 Homebrew，`ios-deploy` 会直接「找不到」。按 Apple Silicon / Intel 顺序补齐。
case ":$PATH:" in
  *:/opt/homebrew/bin:*) ;;
  *) PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" ;;
esac
export PATH

if ! command -v ios-deploy >/dev/null 2>&1; then
  echo "错误：找不到 ios-deploy。安装方式：brew install ios-deploy" >&2
  exit 1
fi

# ios-deploy -c 每行形如：
#   [....] Found <40位UDID> (J96AP, iPad mini 4, iphoneos, arm64, 15.8.8, 19H422) a.k.a. 'iPad-mini4' connected through USB.
# 这里只取 "UDID (型号说明)" 这一段。
list_devices() {
  ios-deploy -c -t 6 2>/dev/null \
    | grep -oE 'Found [0-9a-f]{40} \([^)]*\)' \
    | sed 's/^Found //' || true
}

if [ "$LIST_ONLY" = "1" ]; then
  echo "当前通过 USB 连接的 iOS 设备："
  found="$(list_devices)"
  if [ -z "$found" ]; then
    echo "  （无）"
  else
    echo "$found" | sed 's/^/  /'
  fi
  exit 0
fi

# —— 1. 找设备 ——
if [ -z "$UDID" ]; then
  line="$(list_devices | head -1 || true)"
  if [ -z "$line" ]; then
    if [ "$IF_CONNECTED" = "1" ]; then
      echo "（没有检测到 USB 设备，跳过安装）"
      exit 0
    fi
    echo "错误：没有找到通过 USB 连接的 iOS 设备。" >&2
    echo "      请用数据线连上 iPad 并解锁屏幕后重试。" >&2
    exit 1
  fi
  UDID="${line%% *}"
  echo "==> 设备：$line"
else
  echo "==> 设备：$UDID（手动指定）"
fi

# —— 2. 构建 + 签名 ——
# `-Xfrontend -disable-sandbox`：Swift 展开 @State 这类宏会拉 swift-plugin-server，
# 它默认再把自己关进 sandbox-exec 并要写 ~/.swiftpm/security，在受限执行环境里会被拒，
# 报错形如 "SwiftUIMacros.StateMacro could not be found ... malformed response"。
# 细节见 scripts/build-ios.sh 顶部注释。Xcode.app 里 ⌘B 不需要这个开关。
# 产物路径解析顺序：
#   1) --app-path 显式指定
#   2) 命令行构建的固定位置（scripts/build-ios.sh 与本脚本都用 -derivedDataPath build）
#   3) Xcode ⌘B 的 DerivedData —— scheme Post-action 会走这一条，
#      因为 Xcode 不会往 ios/build 里放东西
resolve_app_path() {
  if [ -n "$APP_PATH_OVERRIDE" ]; then
    printf '%s' "$APP_PATH_OVERRIDE"; return
  fi
  local fixed="$IOS_DIR/build/Build/Products/$CONFIG-iphoneos/VoiceTranslator.app"
  local dd
  dd="$(/bin/ls -dt "$HOME"/Library/Developer/Xcode/DerivedData/VoiceTranslator-*/Build/Products/"$CONFIG"-iphoneos/VoiceTranslator.app 2>/dev/null | head -1 || true)"
  # 两个候选都可能存在；按修改时间取更新的那个，
  # 避免 Xcode 刚 ⌘B 完却装到上一次命令行构建的旧产物
  /bin/ls -dt "$fixed" "$dd" 2>/dev/null | head -1 || true
}

if [ "$DO_BUILD" = "1" ]; then
  echo "==> 构建 $CONFIG · iphoneos（自动签名）"
  cd "$IOS_DIR"
  # VT_INSTALL_IPAD_ACTIVE=1：告诉 scheme Post-action「我正在装，别再调回来」
  VT_INSTALL_IPAD_ACTIVE=1 xcodebuild \
    -project VoiceTranslator.xcodeproj \
    -scheme VoiceTranslator \
    -configuration "$CONFIG" \
    -sdk iphoneos \
    -destination 'generic/platform=iOS' \
    -derivedDataPath build \
    -allowProvisioningUpdates \
    OTHER_SWIFT_FLAGS='-Xfrontend -disable-sandbox' \
    build
fi

APP_PATH="$(resolve_app_path)"
if [ -z "$APP_PATH" ] || [ ! -d "$APP_PATH" ]; then
  if [ "$IF_CONNECTED" = "1" ]; then
    echo "（没有找到 iphoneos 构建产物，跳过安装）"
    exit 0
  fi
  echo "错误：找不到 iphoneos 构建产物。" >&2
  echo "      Xcode 里请把运行目标选成「Any iOS Device (arm64)」再 ⌘B；" >&2
  echo "      命令行则去掉 --no-build 先完整跑一次。" >&2
  exit 1
fi

# —— 模拟器守卫 ——
# 刚构建的若是模拟器包（比真机产物新），说明用户此刻在跑模拟器 ——
# 别把上一次的旧真机包灌进 iPad。Post-action 模式下静默跳过，手动模式只警告。
newest_sim="$(/bin/ls -dt \
    "$IOS_DIR"/build/Build/Products/*-iphonesimulator/VoiceTranslator.app \
    "$HOME"/Library/Developer/Xcode/DerivedData/VoiceTranslator-*/Build/Products/*-iphonesimulator/VoiceTranslator.app \
    2>/dev/null | head -1 || true)"
if [ -n "$newest_sim" ] && [ "$newest_sim" -nt "$APP_PATH" ]; then
  if [ "$IF_CONNECTED" = "1" ]; then
    echo "（最近一次构建是模拟器版本，跳过安装）"
    exit 0
  fi
  echo "警告：最近一次构建是模拟器版本（$newest_sim 比真机产物新），本次装的可能是旧包。" >&2
fi

# 顺带把产物信息打出来，便于确认装的是哪一份。
echo "==> 产物：$APP_PATH"
echo "    架构 $(lipo -archs "$APP_PATH/VoiceTranslator" 2>/dev/null || echo '?')" \
     "· 最低系统 $(plutil -extract MinimumOSVersion raw "$APP_PATH/Info.plist" 2>/dev/null || echo '?')"

# —— 3. 安装 ——
# 注意：安装流程末尾经常出现
#     Error 0xe800002e: Could not receive a message from the device. AMDeviceLookupApplications
# 那是 ios-deploy 装完之后回查应用列表超时（设备忙或锁屏），**不影响安装结果**，
# 所以这里吞掉退出码，改由第 4 步的 `-e` 校验给出权威结论。
echo "==> 安装到设备"
ios-deploy -i "$UDID" -b "$APP_PATH" || true

# —— 4. 校验 ——
echo "==> 校验安装结果"
if [ "$(ios-deploy -i "$UDID" -1 "$BUNDLE_ID" -e 2>/dev/null | tail -1)" = "true" ]; then
  echo "✔ $BUNDLE_ID 已安装（$CONFIG 构建）"
  echo "  接下来请在 iPad 上点开 App。"
  echo "  首次启动若提示「未受信任的开发者」，去 设置 → 通用 → VPN与设备管理 里信任该证书。"
else
  echo "✘ 校验失败：设备上没有找到 $BUNDLE_ID" >&2
  echo "  常见原因：iPad 锁屏、或安装过程中被拔线。" >&2
  exit 1
fi
