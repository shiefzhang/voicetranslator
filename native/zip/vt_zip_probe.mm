// vt_zip_probe —— 直接驱动 iOS 的 VTZipArchive 去解真实/合成的 .vtmodel。
//
// 为什么需要它：iOS 侧用的是自研随机访问 ZIP64 读器（Bridge/VTZip.mm），
// 它既不被 App 的单测覆盖，也没法在 Android 侧验证。「中央目录偏移越过 2 GiB
// 还能不能读」这件事，只有真的拿一个这样的包喂给它才算数。
//
// 这个探针把 VTZip.mm **原样**编进来跑，流程与 ModelPackages.swift 的
// `install(from:kind:progress:)` 一致：
//   打开 → 读 manifest.json → 逐条按清单声明的 size + SHA-256 解出。
// 两者差一步都不算通过：少一条就等于 App 会少解一个文件。
//
// 编译与运行见 scripts/check-zip-reader.py。
//
// 输出是机器可读的（每行 `VT_ZIP_PROBE ...`），便于给校验脚本做门禁。

#import <Foundation/Foundation.h>
#import "VTZip.h"

#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <string>
#include <vector>

namespace {

struct Options {
    std::vector<std::string> packages;
    std::string outDir;
    long long skipOver = 0;   // >0 时跳过大于该字节数的条目（默认 0 = 不跳）
    bool keep = false;        // 默认解出后即删，避免把 2 GB 留在盘上
    bool strict = true;       // 清单里有条目没解出来就算失败
};

long long nowMs() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

NSString *toNSString(const std::string &s) {
    return [NSString stringWithUTF8String:s.c_str()];
}

int fail(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::fprintf(stderr, "VT_ZIP_PROBE_ERROR ");
    std::vfprintf(stderr, fmt, ap);
    std::fprintf(stderr, "\n");
    va_end(ap);
    return 2;
}

}  // namespace

int main(int argc, char **argv) {
    @autoreleasepool {
        Options opt;
        for (int i = 1; i < argc; i++) {
            std::string a = argv[i];
            if (a == "--out" && i + 1 < argc) {
                opt.outDir = argv[++i];
            } else if (a == "--skip-over" && i + 1 < argc) {
                opt.skipOver = std::stoll(argv[++i]);
            } else if (a == "--keep") {
                opt.keep = true;
            } else if (a.rfind("--", 0) == 0) {
                return fail("未知参数 %s", a.c_str());
            } else {
                opt.packages.push_back(a);
            }
        }
        if (opt.packages.empty() || opt.outDir.empty()) {
            return fail("用法：vt_zip_probe --out <目录> [--skip-over N] [--keep] <包> [包…]");
        }

        NSFileManager *fm = [NSFileManager defaultManager];
        NSError *dirError = nil;
        if (![fm createDirectoryAtPath:toNSString(opt.outDir)
           withIntermediateDirectories:YES
                            attributes:nil
                                 error:&dirError]) {
            return fail("无法创建输出目录 %s：%s", opt.outDir.c_str(),
                        dirError.localizedDescription.UTF8String);
        }

        int failures = 0;
        for (const std::string &pkg : opt.packages) {
            NSString *path = toNSString(pkg);
            NSDictionary *attrs = [fm attributesOfItemAtPath:path error:nil];
            unsigned long long fileSize = [attrs fileSize];

            NSError *error = nil;
            long long t0 = nowMs();
            VTZipArchive *archive = [VTZipArchive archiveAtPath:path error:&error];
            if (archive == nil) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 打开失败：%s\n",
                             pkg.c_str(), error.localizedDescription.UTF8String);
                failures++;
                continue;
            }
            long long openMs = nowMs() - t0;

            NSArray<NSString *> *names = archive.entryNames;
            std::printf("VT_ZIP_PROBE package=%s file_bytes=%llu entries=%lu open_ms=%lld\n",
                        pkg.c_str(), fileSize, (unsigned long)names.count, openMs);

            // 条目名必须全部通过路径安全判定 —— 与两端 App 的导入校验同一条规则。
            for (NSString *name in names) {
                if (!VTSafeArchivePath(name)) {
                    std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 不安全条目名 %s\n",
                                 pkg.c_str(), name.UTF8String);
                    failures++;
                }
            }

            // 清单：这是 App 拿 size / sha256 的唯一来源。
            NSData *manifestData = [archive readEntryNamed:@"manifest.json" error:&error];
            if (manifestData == nil) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 读不到 manifest.json：%s\n",
                             pkg.c_str(), error.localizedDescription.UTF8String);
                failures++;
                continue;
            }
            id parsed = [NSJSONSerialization JSONObjectWithData:manifestData options:0 error:&error];
            if (![parsed isKindOfClass:[NSDictionary class]]) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: manifest.json 不是合法 JSON：%s\n",
                             pkg.c_str(), error.localizedDescription.UTF8String);
                failures++;
                continue;
            }
            NSArray *files = ((NSDictionary *)parsed)[@"files"];
            if (![files isKindOfClass:[NSArray class]]) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: manifest.files 缺失\n", pkg.c_str());
                failures++;
                continue;
            }

            unsigned long extracted = 0, skipped = 0;
            unsigned long long totalBytes = 0;
            for (NSDictionary *spec in files) {
                NSString *rel = spec[@"path"];
                NSNumber *sizeNum = spec[@"size"];
                NSString *sha = spec[@"sha256"];
                if (rel == nil || sizeNum == nil || sha == nil) {
                    std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 清单条目字段不全\n", pkg.c_str());
                    failures++;
                    continue;
                }
                unsigned long long size = sizeNum.unsignedLongLongValue;

                if (opt.skipOver > 0 && size > (unsigned long long)opt.skipOver) {
                    std::printf("VT_ZIP_PROBE_ENTRY package=%s path=%s size=%llu skipped=1\n",
                                pkg.c_str(), rel.UTF8String, size);
                    skipped++;
                    continue;
                }

                NSString *dest = [toNSString(opt.outDir)
                        stringByAppendingPathComponent:
                                [NSString stringWithFormat:@"%08llu-%@",
                                 (unsigned long long)extracted, [rel lastPathComponent]]];
                long long t1 = nowMs();
                BOOL ok = [archive extractEntryNamed:rel
                                              toFile:dest
                                        expectedSize:size
                                      expectedSHA256:sha
                                               error:&error];
                long long ms = nowMs() - t1;
                if (!ok) {
                    std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 解包失败 %s：%s\n",
                                 pkg.c_str(), rel.UTF8String,
                                 error.localizedDescription.UTF8String);
                    failures++;
                    continue;
                }
                std::printf("VT_ZIP_PROBE_ENTRY package=%s path=%s size=%llu skipped=0 ms=%lld\n",
                            pkg.c_str(), rel.UTF8String, size, ms);
                totalBytes += size;
                extracted++;
                if (!opt.keep) [fm removeItemAtPath:dest error:nil];
            }

            // 条目数必须与「清单 + manifest.json」完全对上：多一个或少一个都要报。
            unsigned long expectedEntries = files.count + 1;
            if (names.count != expectedEntries) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 条目数 %lu != 清单 %lu + 1\n",
                             pkg.c_str(), (unsigned long)names.count, (unsigned long)files.count);
                failures++;
            }
            if (opt.strict && skipped > 0) {
                std::fprintf(stderr, "VT_ZIP_PROBE_ERROR %s: 有 %lu 个条目被 --skip-over 跳过，"
                                     "这条运行不能当作通过\n", pkg.c_str(), skipped);
                failures++;
            }
            std::printf("VT_ZIP_PROBE_DONE package=%s extracted=%lu skipped=%lu bytes=%llu\n",
                        pkg.c_str(), extracted, skipped, totalBytes);
        }

        std::printf("VT_ZIP_PROBE_SUMMARY failures=%d packages=%zu\n", failures, opt.packages.size());
        return failures == 0 ? 0 : 1;
    }
}
