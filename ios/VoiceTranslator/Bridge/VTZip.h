#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// 流式 ZIP / ZIP64 读取器。
///
/// 移植设计书 §5.2 的关键兼容性要求：
/// TranslateGemma Q4 包约 2.49GB，普通 ZIP 的中央目录偏移超过 2GB 时
/// Android 会报 `invalid cen header (bad signature)`。因此 iOS 侧解包器
/// **必须支持 ZIP64**，且必须能流式处理大文件（不能整包读进内存）。
///
/// 本实现用 zlib 做流式 inflate，用 CommonCrypto 做流式 SHA-256，
/// 与 Android `ModelPackages.installLargeZip` 的行为一一对应。
@interface VTZipArchive : NSObject

/// 打开压缩包并解析中央目录（含 ZIP64 记录）。
+ (nullable instancetype)archiveAtPath:(NSString *)path error:(NSError **)error;

/// 中央目录中声明的全部条目名（用于校验"未声明的文件"）。
@property (nonatomic, readonly, copy) NSArray<NSString *> *entryNames;

/// 条目声明的原始大小；不存在时返回 -1。
- (long long)uncompressedSizeOfEntry:(NSString *)name;

/// 读取小条目（清单文件用）。
- (nullable NSData *)readEntryNamed:(NSString *)name error:(NSError **)error;

/// 流式解压单个条目到目标文件，同时校验大小与 SHA-256。
- (BOOL)extractEntryNamed:(NSString *)name
                   toFile:(NSString *)destination
             expectedSize:(unsigned long long)size
           expectedSHA256:(nullable NSString *)sha256
                    error:(NSError **)error;

@end

/// 路径安全判定，对齐 Android `ModelPackages.safe`：
/// 拒绝绝对路径、反斜杠、冒号、空段、`.`、`..`。
///
/// ⚠️ 这两个自由函数必须包在 `extern "C"` 里。
/// 本头会被 Objective-C++（VTZip.mm）包含，若不加，它们会按 C++ 规则改名
/// （`__Z18VTSafeArchivePathP8NSString`），而 Swift 通过桥接头是按 C 规则
/// 找符号，链接期就会报 `Undefined symbols: _VTSafeArchivePath`。
/// `@interface` 里的 ObjC 方法不受影响（走 ObjC runtime），只有自由函数需要。
#ifdef __cplusplus
extern "C" {
#endif

BOOL VTSafeArchivePath(NSString *name);

/// 文件 SHA-256（小写十六进制）。
NSString *VTSHA256OfFile(NSString *path);

#ifdef __cplusplus
}
#endif

NS_ASSUME_NONNULL_END
