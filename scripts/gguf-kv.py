#!/usr/bin/env python3
"""读 GGUF 的 KV 元数据（含超长的 tokenizer.chat_template）。

用法：
    gguf-kv.py <model.gguf> [key ...]
    gguf-kv.py <model.gguf> --list          # 只列 key
不带 key 时打印全部（chat_template 会截断显示，用 --full 看全文）。

为什么单独写一个：GGUF 的 KV 区有 13 种类型 + 数组嵌套，写错一个偏移后续全废。
之前用 inline 的临时脚本解析就踩过这个坑。
"""
import struct
import sys
from pathlib import Path

# gguf 的 GGUF_METADATA_VALUE_TYPE —— 顺序**不能凭记忆**，STRING=8 / ARRAY=9：
#   UINT8=0 INT8=1 UINT16=2 INT16=3 UINT32=4 INT32=5 FLOAT32=6 BOOL=7
#   STRING=8 ARRAY=9 UINT64=10 INT64=11 FLOAT64=12
U8, I8, U16, I16, U32, I32, F32, BOOL, STR, ARR, U64, I64, F64 = range(13)


def _read_str(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def _read_value(f, t):
    if t == U8:
        return f.read(1)[0]
    if t == I8:
        return struct.unpack("<b", f.read(1))[0]
    if t == U16:
        return struct.unpack("<H", f.read(2))[0]
    if t == I16:
        return struct.unpack("<h", f.read(2))[0]
    if t == U32:
        return struct.unpack("<I", f.read(4))[0]
    if t == I32:
        return struct.unpack("<i", f.read(4))[0]
    if t == F32:
        return struct.unpack("<f", f.read(4))[0]
    if t == BOOL:
        return bool(f.read(1)[0])
    if t == STR:
        return _read_str(f)
    if t == U64:
        return struct.unpack("<Q", f.read(8))[0]
    if t == I64:
        return struct.unpack("<q", f.read(8))[0]
    if t == F64:
        return struct.unpack("<d", f.read(8))[0]
    if t == ARR:
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        # 大数组只回类型与长度，避免把 15 万个 token 全读进内存
        if et == STR or n > 64:
            if et == STR:
                for _ in range(n):
                    _read_str(f)
            else:
                for _ in range(n):
                    _read_value(f, et)
            return f"<{n} 项>"
        return [_read_value(f, et) for _ in range(n)]
    raise ValueError(f"未知类型 {t}")


def read_kv(path, wanted=None):
    out = {}
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError(f"不是 GGUF：magic={magic!r}")
        version = struct.unpack("<I", f.read(4))[0]
        n_tensors, n_kv = struct.unpack("<QQ", f.read(16))
        for _ in range(n_kv):
            k = _read_str(f)
            t = struct.unpack("<I", f.read(4))[0]
            if wanted is None or k in wanted:
                out[k] = _read_value(f, t)
            else:
                _skip(f, t)
    return version, n_tensors, out


def _skip(f, t):
    _SKIP = {U8: 1, I8: 1, BOOL: 1, U16: 2, I16: 2, U32: 4, I32: 4, F32: 4,
             U64: 8, I64: 8, F64: 8}
    if t in _SKIP:
        f.seek(_SKIP[t], 1)
    elif t == STR:
        f.seek(struct.unpack("<Q", f.read(8))[0], 1)
    elif t == ARR:
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        if et in _SKIP:
            f.seek(_SKIP[et] * n, 1)
        else:
            for _ in range(n):
                _skip(f, et)
    else:
        raise ValueError(f"未知类型 {t}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    rest = sys.argv[2:]
    full = "--full" in rest
    list_only = "--list" in rest
    keys = [a for a in rest if not a.startswith("--")]

    version, n_tensors, kv = read_kv(path, set(keys) or None)
    print(f"{path.name}：version={version} tensors={n_tensors} kv={len(kv)}")
    if list_only:
        for k in kv:
            print("  ", k)
        return 0
    for k, v in kv.items():
        s = str(v)
        if len(s) > 300 and not full:
            s = s[:300] + f"…（共 {len(s)} 字符，加 --full 看全文）"
        print(f"  {k} = {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
