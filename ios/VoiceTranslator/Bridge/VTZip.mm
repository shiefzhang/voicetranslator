#import "VTZip.h"

#import <CommonCrypto/CommonDigest.h>
#import <zlib.h>

#include <cstring>
#include <map>
#include <string>
#include <vector>

namespace {

constexpr uint32_t kLFH = 0x04034b50;
constexpr uint32_t kCDFH = 0x02014b50;
constexpr uint32_t kEOCD = 0x06054b50;
constexpr uint32_t kZip64EOCDLocator = 0x07064b50;
constexpr uint32_t kZip64EOCD = 0x06064b50;
constexpr uint32_t kZIP64ExtraID = 0x0001;
constexpr uint64_t kU32Max = 0xFFFFFFFFULL;
constexpr uint64_t kU16Max = 0xFFFFULL;

NSError *vtError(NSString *message) {
    return [NSError errorWithDomain:@"VoiceTranslator.Zip"
                               code:1
                           userInfo:@{NSLocalizedDescriptionKey: message}];
}

inline uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
inline uint32_t rd32(const uint8_t *p) {
    return (uint32_t)(p[0] | (p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24));
}
inline uint64_t rd64(const uint8_t *p) {
    return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

struct Entry {
    std::string name;
    uint16_t method = 0;
    uint16_t flags = 0;
    uint64_t compressedSize = 0;
    uint64_t uncompressedSize = 0;
    uint64_t localHeaderOffset = 0;
};

}  // namespace

BOOL VTSafeArchivePath(NSString *name) {
    if (name.length == 0) return NO;
    if ([name hasPrefix:@"/"]) return NO;
    if ([name containsString:@"\\"]) return NO;
    if ([name containsString:@":"]) return NO;
    for (NSString *part in [name componentsSeparatedByString:@"/"]) {
        if (part.length == 0) return NO;
        if ([part isEqualToString:@"."]) return NO;
        if ([part isEqualToString:@".."]) return NO;
    }
    return YES;
}

NSString *VTSHA256OfFile(NSString *path) {
    NSFileHandle *handle = [NSFileHandle fileHandleForReadingAtPath:path];
    if (handle == nil) return nil;
    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    @try {
        while (YES) {
            @autoreleasepool {
                NSData *chunk = [handle readDataOfLength:1 << 20];
                if (chunk.length == 0) break;
                CC_SHA256_Update(&ctx, chunk.bytes, (CC_LONG)chunk.length);
            }
        }
    } @catch (__unused NSException *e) {
        [handle closeFile];
        return nil;
    }
    [handle closeFile];
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &ctx);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", digest[i]];
    return hex;
}

@implementation VTZipArchive {
    NSFileHandle *_file;
    unsigned long long _fileSize;
    std::map<std::string, Entry> _entries;
}

+ (nullable instancetype)archiveAtPath:(NSString *)path error:(NSError **)error {
    VTZipArchive *archive = [[VTZipArchive alloc] init];
    if (![archive openAtPath:path error:error]) return nil;
    return archive;
}

- (BOOL)openAtPath:(NSString *)path error:(NSError **)error {
    _file = [NSFileHandle fileHandleForReadingAtPath:path];
    if (_file == nil) {
        if (error) *error = vtError(@"无法打开模型包文件");
        return NO;
    }
    NSDictionary *attrs = [[NSFileManager defaultManager] attributesOfItemAtPath:path error:nil];
    _fileSize = [attrs fileSize];

    // —— 定位 EOCD：从尾部向前扫描签名（注释最长 65535 字节）——
    const unsigned long long tailWanted = std::min<unsigned long long>(_fileSize, 65557);
    if (tailWanted < 22) {
        if (error) *error = vtError(@"模型包过小或已损坏");
        return NO;
    }
    [_file seekToFileOffset:_fileSize - tailWanted];
    NSData *tail = [_file readDataOfLength:(NSUInteger)tailWanted];
    const uint8_t *bytes = (const uint8_t *)tail.bytes;
    long long eocdPos = -1;
    for (long long i = (long long)tail.length - 22; i >= 0; i--) {
        if (rd32(bytes + i) == kEOCD) {
            eocdPos = (long long)(_fileSize - tailWanted) + i;
            break;
        }
    }
    if (eocdPos < 0) {
        if (error) *error = vtError(@"模型包格式不兼容，请重新获取 ZIP64 包");
        return NO;
    }
    const uint8_t *eocd = bytes + (eocdPos - (long long)(_fileSize - tailWanted));
    uint64_t entryCount = rd16(eocd + 10);
    uint64_t cdSize = rd32(eocd + 12);
    uint64_t cdOffset = rd32(eocd + 16);

    // —— ZIP64：任一字段溢出时，读 EOCD 之前的 ZIP64 定位器 ——
    if (entryCount == kU16Max || cdSize == kU32Max || cdOffset == kU32Max) {
        if (eocdPos < 20) {
            if (error) *error = vtError(@"ZIP64 定位器缺失");
            return NO;
        }
        [_file seekToFileOffset:(unsigned long long)eocdPos - 20];
        NSData *locator = [_file readDataOfLength:20];
        if (locator.length < 20 || rd32((const uint8_t *)locator.bytes) != kZip64EOCDLocator) {
            if (error) *error = vtError(@"ZIP64 定位器无效");
            return NO;
        }
        uint64_t zip64EOCDOffset = rd64((const uint8_t *)locator.bytes + 8);
        if (zip64EOCDOffset >= _fileSize) {
            if (error) *error = vtError(@"ZIP64 记录位置越界");
            return NO;
        }
        [_file seekToFileOffset:zip64EOCDOffset];
        NSData *record = [_file readDataOfLength:56];
        if (record.length < 56 || rd32((const uint8_t *)record.bytes) != kZip64EOCD) {
            if (error) *error = vtError(@"ZIP64 记录无效");
            return NO;
        }
        const uint8_t *z = (const uint8_t *)record.bytes;
        entryCount = rd64(z + 32);
        cdSize = rd64(z + 40);
        cdOffset = rd64(z + 48);
    }

    if (cdOffset + cdSize > _fileSize || cdSize > (64ULL << 20)) {
        if (error) *error = vtError(@"中央目录越界");
        return NO;
    }

    // —— 解析中央目录 ——
    [_file seekToFileOffset:cdOffset];
    NSData *cd = [_file readDataOfLength:(NSUInteger)cdSize];
    if ((uint64_t)cd.length != cdSize) {
        if (error) *error = vtError(@"中央目录读取不完整");
        return NO;
    }
    const uint8_t *p = (const uint8_t *)cd.bytes;
    const uint8_t *end = p + cd.length;
    uint64_t parsed = 0;
    while (p + 46 <= end && rd32(p) == kCDFH) {
        Entry entry;
        entry.flags = rd16(p + 8);
        entry.method = rd16(p + 10);
        entry.compressedSize = rd32(p + 20);
        entry.uncompressedSize = rd32(p + 24);
        uint16_t nameLen = rd16(p + 28);
        uint16_t extraLen = rd16(p + 30);
        uint16_t commentLen = rd16(p + 32);
        entry.localHeaderOffset = rd32(p + 42);

        const uint8_t *namePtr = p + 46;
        if (namePtr + nameLen > end) break;
        entry.name.assign((const char *)namePtr, nameLen);

        // ZIP64 扩展字段：只补齐那些被写成 0xFFFFFFFF 的字段，顺序固定。
        const uint8_t *extra = namePtr + nameLen;
        const uint8_t *extraEnd = extra + extraLen;
        if (extraEnd <= end) {
            const uint8_t *q = extra;
            while (q + 4 <= extraEnd) {
                uint16_t id = rd16(q);
                uint16_t len = rd16(q + 2);
                const uint8_t *body = q + 4;
                if (body + len > extraEnd) break;
                if (id == kZIP64ExtraID) {
                    const uint8_t *r = body;
                    const uint8_t *rEnd = body + len;
                    if (entry.uncompressedSize == kU32Max && r + 8 <= rEnd) {
                        entry.uncompressedSize = rd64(r);
                        r += 8;
                    }
                    if (entry.compressedSize == kU32Max && r + 8 <= rEnd) {
                        entry.compressedSize = rd64(r);
                        r += 8;
                    }
                    if (entry.localHeaderOffset == kU32Max && r + 8 <= rEnd) {
                        entry.localHeaderOffset = rd64(r);
                        r += 8;
                    }
                }
                q = body + len;
            }
        }
        _entries[entry.name] = entry;
        parsed++;
        p = namePtr + nameLen + extraLen + commentLen;
    }
    if (parsed == 0 || (entryCount != 0 && parsed != entryCount)) {
        if (error) *error = vtError(@"中央目录解析失败");
        return NO;
    }
    return YES;
}

- (NSArray<NSString *> *)entryNames {
    NSMutableArray<NSString *> *names = [NSMutableArray arrayWithCapacity:_entries.size()];
    for (const auto &pair : _entries) {
        [names addObject:[NSString stringWithUTF8String:pair.first.c_str()]];
    }
    return names;
}

- (long long)uncompressedSizeOfEntry:(NSString *)name {
    const char *key = name.UTF8String;
    if (key == nullptr) return -1;
    auto it = _entries.find(std::string(key));
    if (it == _entries.end()) return -1;
    return (long long)it->second.uncompressedSize;
}

- (nullable NSData *)readEntryNamed:(NSString *)name error:(NSError **)error {
    NSMutableData *out = [NSMutableData data];
    if (![self streamEntryNamed:name
                      toCallback:^BOOL(const uint8_t *bytes, size_t length, NSError **cbError) {
                          [out appendBytes:bytes length:length];
                          return YES;
                      }
                           error:error]) {
        return nil;
    }
    return out;
}

- (BOOL)extractEntryNamed:(NSString *)name
                   toFile:(NSString *)destination
             expectedSize:(unsigned long long)size
           expectedSHA256:(nullable NSString *)sha256
                    error:(NSError **)error {
    // 这两者都会在块内被累加，必须加 __block。
    // 否则块捕获的是 const 拷贝，`&ctx` 会退化成 `const CC_SHA256_CTX *`，
    // CC_SHA256_Update 的形参不匹配而编译失败。
    __block CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    __block unsigned long long written = 0;

    if (![[NSFileManager defaultManager] createFileAtPath:destination contents:nil attributes:nil]) {
        if (error) *error = vtError([NSString stringWithFormat:@"无法创建文件 %@", destination]);
        return NO;
    }
    NSFileHandle *out = [NSFileHandle fileHandleForWritingAtPath:destination];
    if (out == nil) {
        if (error) *error = vtError([NSString stringWithFormat:@"无法写入 %@", destination]);
        return NO;
    }

    BOOL ok = [self streamEntryNamed:name
                          toCallback:^BOOL(const uint8_t *bytes, size_t length, NSError **cbError) {
                              written += length;
                              if (written > size) {
                                  if (cbError) *cbError = vtError(@"解包越界");
                                  return NO;
                              }
                              CC_SHA256_Update(&ctx, bytes, (CC_LONG)length);
                              @try {
                                  [out writeData:[NSData dataWithBytesNoCopy:(void *)bytes
                                                                      length:length
                                                                freeWhenDone:NO]];
                              } @catch (NSException *e) {
                                  if (cbError) *cbError = vtError(@"写入失败");
                                  return NO;
                              }
                              return YES;
                          }
                               error:error];
    [out closeFile];

    if (!ok) return NO;
    if (written != size) {
        if (error) *error = vtError([NSString stringWithFormat:@"文件大小不符：%@", name]);
        return NO;
    }
    if (sha256 != nil) {
        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256_Final(digest, &ctx);
        NSMutableString *hex = [NSMutableString stringWithCapacity:64];
        for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", digest[i]];
        if (![hex isEqualToString:sha256]) {
            if (error) *error = vtError([NSString stringWithFormat:@"校验失败：%@", name]);
            return NO;
        }
    }
    return YES;
}

/// 统一的流式解压内核：store 直接透传，deflate 走 zlib 原始 inflate。
- (BOOL)streamEntryNamed:(NSString *)name
              toCallback:(BOOL (^)(const uint8_t *bytes, size_t length, NSError **error))callback
                   error:(NSError **)error {
    const char *key = name.UTF8String;
    if (key == nullptr) {
        if (error) *error = vtError(@"条目名无效");
        return NO;
    }
    auto it = _entries.find(std::string(key));
    if (it == _entries.end()) {
        if (error) *error = vtError([NSString stringWithFormat:@"压缩包缺少 %@", name]);
        return NO;
    }
    const Entry &entry = it->second;
    if ((entry.flags & 0x0001) != 0) {
        if (error) *error = vtError(@"不支持加密的模型包");
        return NO;
    }

    // —— 本地文件头 ——
    [_file seekToFileOffset:entry.localHeaderOffset];
    NSData *header = [_file readDataOfLength:30];
    if (header.length < 30 || rd32((const uint8_t *)header.bytes) != kLFH) {
        if (error) *error = vtError([NSString stringWithFormat:@"本地文件头无效：%@", name]);
        return NO;
    }
    uint16_t localNameLen = rd16((const uint8_t *)header.bytes + 26);
    uint16_t localExtraLen = rd16((const uint8_t *)header.bytes + 28);
    unsigned long long dataOffset = entry.localHeaderOffset + 30 + localNameLen + localExtraLen;

    if (entry.method == 0) {
        // 存储：分块读取并回调。
        [_file seekToFileOffset:dataOffset];
        uint64_t remaining = entry.compressedSize;
        while (remaining > 0) {
            @autoreleasepool {
                NSUInteger want = (NSUInteger)std::min<uint64_t>(remaining, 1 << 20);
                NSData *chunk = [_file readDataOfLength:want];
                if (chunk.length == 0) {
                    if (error) *error = vtError(@"数据被截断");
                    return NO;
                }
                NSError *cbError = nil;
                if (!callback((const uint8_t *)chunk.bytes, chunk.length, &cbError)) {
                    if (error) *error = cbError ?: vtError(@"解包中止");
                    return NO;
                }
                remaining -= chunk.length;
            }
        }
        return YES;
    }

    if (entry.method != 8) {
        if (error) *error = vtError(@"不支持的压缩方式");
        return NO;
    }

    // —— deflate（raw，-MAX_WBITS）——
    [_file seekToFileOffset:dataOffset];
    z_stream strm;
    std::memset(&strm, 0, sizeof(strm));
    if (inflateInit2(&strm, -MAX_WBITS) != Z_OK) {
        if (error) *error = vtError(@"初始化解压器失败");
        return NO;
    }
    std::vector<uint8_t> inBuf(1 << 20);
    std::vector<uint8_t> outBuf(1 << 20);
    uint64_t remaining = entry.compressedSize;
    int status = Z_OK;
    BOOL failed = NO;

    while (remaining > 0 && !failed) {
        @autoreleasepool {
            NSUInteger want = (NSUInteger)std::min<uint64_t>(remaining, inBuf.size());
            NSData *chunk = [_file readDataOfLength:want];
            if (chunk.length == 0) {
                if (error) *error = vtError(@"数据被截断");
                failed = YES;
                break;
            }
            remaining -= chunk.length;
            std::memcpy(inBuf.data(), chunk.bytes, chunk.length);
            strm.next_in = inBuf.data();
            strm.avail_in = (uInt)chunk.length;

            while (strm.avail_in > 0 && !failed) {
                strm.next_out = outBuf.data();
                strm.avail_out = (uInt)outBuf.size();
                status = inflate(&strm, Z_NO_FLUSH);
                if (status != Z_OK && status != Z_STREAM_END && status != Z_BUF_ERROR) {
                    if (error) *error = vtError(@"解压失败，模型包可能已损坏");
                    failed = YES;
                    break;
                }
                size_t produced = outBuf.size() - strm.avail_out;
                if (produced > 0) {
                    NSError *cbError = nil;
                    if (!callback(outBuf.data(), produced, &cbError)) {
                        if (error) *error = cbError ?: vtError(@"解包中止");
                        failed = YES;
                        break;
                    }
                }
                if (status == Z_STREAM_END) break;
            }
        }
    }

    // 收尾：把 strm 里可能残留的输出冲干净。
    if (!failed) {
        while (true) {
            strm.next_out = outBuf.data();
            strm.avail_out = (uInt)outBuf.size();
            status = inflate(&strm, Z_FINISH);
            size_t produced = outBuf.size() - strm.avail_out;
            if (produced > 0) {
                NSError *cbError = nil;
                if (!callback(outBuf.data(), produced, &cbError)) {
                    if (error) *error = cbError ?: vtError(@"解包中止");
                    failed = YES;
                    break;
                }
            }
            if (status == Z_STREAM_END) break;
            if (status != Z_OK && status != Z_BUF_ERROR) {
                if (error) *error = vtError(@"解压未正常结束");
                failed = YES;
                break;
            }
            if (produced == 0) break;
        }
    }
    inflateEnd(&strm);
    return !failed;
}

- (void)dealloc {
    [_file closeFile];
}

@end
