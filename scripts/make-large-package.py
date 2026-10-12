#!/usr/bin/env python3
"""合成一个「中央目录偏移越过 2 GiB」的 `.vtmodel`。

## 为什么需要它

真实包里最大的一档是 TranslateGemma 4B Q3_K_M，2,098,495,017 字节，中央目录偏移
**2,098,495,0xx** —— 距 `2^31`（2,147,483,648）只剩约 49 MB。也就是说本机**没有任何
真实包会踩到那条悬崖**，而「>2 GiB 会不会炸」恰恰是要验的东西。等真有 2.5 GB 的包
再验，就等于把风险留到线上。

所以造一个：在文件开头铺一段 **稀疏**（不占磁盘块）的 0 填充，让第一个本地文件头
落在 `2^31` 之后，中央目录偏移自然越界。稀疏填充让这个「2.15 GB 的包」在磁盘上
只占几 KB —— `du` 会显示 0，`ls` 会显示 2.15 GB。

## 结构与 model_pack.py 的产物一致

    0 .. GAP-1        稀疏 0 填充（不是合法 ZIP 结构的一部分，像自解压外壳）
    GAP               本地文件头 manifest.json  → 数据
                      本地文件头 licenses/LICENSE.txt → 数据
                      本地文件头 model.gguf → 数据
                      中央目录（每个条目都带 ZIP64 扩展字段）
                      ZIP64 EOCD 记录
                      ZIP64 EOCD 定位器
                      EOCD（三个字段全部写哨兵值）

每个条目的本地头都把 size / csize 写成 `0xFFFFFFFF`、真值放 0x0001 扩展字段 ——
与 `model_pack.py` 强制 ZIP64 之后的实际产物一模一样。这样 Android 的
`VtStoredZip` 与 iOS 的 `VTZipArchive` 走的都是真实分支，而不是一条只有测试才走的路。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

# 越过 2^31 之后再多留一点，确保是「真的越过了」而不是卡在边界上。
CLIFF = 0x80000000
DEFAULT_GAP = CLIFF + 8 * 1024 * 1024

LOCAL_SIG = 0x04034B50
CD_SIG = 0x02014B50
ZIP64_EOCD_SIG = 0x06064B50
ZIP64_LOCATOR_SIG = 0x07064B50
EOCD_SIG = 0x06054B50
U32_MAX = 0xFFFFFFFF
U16_MAX = 0xFFFF
VERSION_ZIP64 = 45


def _local_header(name: bytes, size: int, crc: int) -> bytes:
    """本地文件头 + ZIP64 扩展字段。size/csize 一律写哨兵，真值放扩展字段。"""
    extra = struct.pack('<HHQQ', 0x0001, 16, size, size)
    return (struct.pack('<IHHHHHIIIHH', LOCAL_SIG, VERSION_ZIP64, 0, 0, 0, 0,
                        crc, U32_MAX, U32_MAX, len(name), len(extra))
            + name + extra)


def _cd_entry(name: bytes, size: int, crc: int, offset: int) -> bytes:
    """中央目录项 + ZIP64 扩展字段（顺序固定：uncompressed, compressed, offset）。"""
    extra = struct.pack('<HHQQQ', 0x0001, 24, size, size, offset)
    return (struct.pack('<IHHHHHHIIIHHHHHII', CD_SIG, VERSION_ZIP64, VERSION_ZIP64,
                        0, 0, 0, 0, crc, U32_MAX, U32_MAX, len(name), len(extra),
                        0, 0, 0, 0, U32_MAX)
            + name + extra)


def build(target: Path, gap: int = DEFAULT_GAP, payload_bytes: int = 4096) -> dict:
    """合成包并返回自述信息（含 cdOffset 是否真的越过 2^31）。"""

    def payload(seed: bytes, length: int) -> bytes:
        """确定性、不压缩的测试负载（STORED 下就是原样字节）。"""
        return (seed * ((length // len(seed)) + 1))[:length]

    files = {
        'licenses/LICENSE.txt': payload(b'VT-LICENSE-', 4096),
        'model.gguf': (b'GGUF' + bytes(range(256)) * ((payload_bytes // 256) + 1))[:payload_bytes],
    }
    manifest = {
        'schemaVersion': 1,
        'id': 'synthetic-large-vtmodel-v1',
        'name': '合成大包（中央目录偏移 > 2 GiB）',
        'version': '1.0.0',
        'kind': 'translation',
        'engine': 'llama-qwen2',
        'runtime': 'llama.cpp b9631',
        'languages': ['zh', 'ja', 'ko', 'en', 'yue'],
        'contextSize': 2048,
        'source': 'synthetic://make-large-package.py',
        # manifest.json 自己不进 files；size/sha256 由真实负载现算。
        'files': [
            {'path': name,
             'size': len(data),
             'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    return _write_ordered(target, gap, manifest, files)


def _write_ordered(target: Path, gap: int, manifest: dict, files: dict) -> dict:
    """按「manifest.json 在最前」的真实顺序一次成型。

    条目的顺序与偏移必须在同一次遍历里定下来 —— 先写数据再回头插 manifest
    会因为本地头长度不同而让所有偏移错位（而且还得搬 2 GB）。
    """
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    ordered = [('manifest.json', manifest_bytes)] + sorted(files.items())

    if target.exists():
        target.unlink()
    entries = []
    with open(target, 'wb') as f:
        f.seek(gap)                      # ← 稀疏空洞，磁盘占用接近 0
        for name, data in ordered:
            offset = f.tell()
            crc = zlib.crc32(data) & 0xFFFFFFFF
            f.write(_local_header(name.encode(), len(data), crc))
            f.write(data)
            entries.append((name.encode(), len(data), crc, offset))

        cd_offset = f.tell()
        for name, size, crc, offset in entries:
            f.write(_cd_entry(name, size, crc, offset))
        cd_size = f.tell() - cd_offset

        zip64_eocd_offset = f.tell()
        f.write(struct.pack('<IQHHIIQQQQ', ZIP64_EOCD_SIG, 44, VERSION_ZIP64, VERSION_ZIP64,
                            0, 0, len(entries), len(entries), cd_size, cd_offset))
        f.write(struct.pack('<IIQI', ZIP64_LOCATOR_SIG, 0, zip64_eocd_offset, 1))
        f.write(struct.pack('<IHHHHIIH', EOCD_SIG, 0, 0, U16_MAX, U16_MAX,
                            U32_MAX, U32_MAX, 0))

    return {
        'path': str(target),
        'fileBytes': target.stat().st_size,
        'diskBytes': _disk_bytes(target),
        'gap': gap,
        'cdOffset': cd_offset,
        'cdSize': cd_size,
        'entries': [n.decode() for n, _, _, _ in entries],
        'crossesCliff': cd_offset > 0x7FFFFFFF,
    }


def _disk_bytes(path: Path) -> int:
    st = path.stat()
    return getattr(st, 'st_blocks', 0) * 512


def main() -> int:
    p = argparse.ArgumentParser(description='合成一个中央目录偏移 > 2 GiB 的 .vtmodel')
    p.add_argument('output', nargs='?', type=Path,
                   default=ROOT / 'dist' / 'large' / 'synthetic-over-2g.vtmodel')
    p.add_argument('--gap', type=int, default=DEFAULT_GAP,
                   help=f'前缀空洞长度（默认 {DEFAULT_GAP}，即 2^31 + 8 MiB）')
    p.add_argument('--payload', type=int, default=4096, help='model.gguf 的字节数（默认 4 KiB）')
    a = p.parse_args()

    info = build(a.output, a.gap, a.payload)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if not info['crossesCliff']:
        print('错误：中央目录偏移没越过 2^31，这个包验不了目标场景', file=sys.stderr)
        return 1
    print(f"\n中央目录偏移 {info['cdOffset']:,} > 2^31 = {0x80000000:,}  ✅ 越过悬崖")
    print(f"文件大小 {info['fileBytes']:,} 字节，实际占用磁盘 {info['diskBytes']:,} 字节（稀疏）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
