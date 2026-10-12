#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ModelScope 分段并行下载器（Range 续传 + SHA256 校验）。

为什么不用 curl：本机走 Clash 代理，hf-mirror 只有 ~0.18 MB/s，
ModelScope 单流 3.8 MB/s 且支持 Range；纯 Python 实现顺便避开
本环境不可靠的 shell 工具（grep/awk 有假阴性）。

用法:
    python3 scripts/fetch-ms.py <repo> <remote_path> <local_path> [选项]

示例:
    python3 scripts/fetch-ms.py Xenova/m2m100_418M \\
        onnx/encoder_model_int8.onnx .tools/m2m/encoder_model_int8.onnx

选项:
    --segments N  并行段数（默认 8）
    --retries  N  每段重试次数（默认 5）
    --sha256   H  期望的 SHA256，下载后校验
    --source   X  通道：ms=ModelScope（默认）/ hf=HuggingFace 镜像
    --mirror   U  覆盖镜像地址
    --quiet       不打印每段进度

示例（HF 通道，Xenova 的 ONNX 只在这里有）:
    python3 scripts/fetch-ms.py --source hf Xenova/opus-mt-zh-en \\
        onnx/encoder_model_int8.onnx .tools/opus/zh-en/encoder_model_int8.onnx
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_MIRROR = "https://modelscope.cn/api/v1/models"
DEFAULT_MIRROR_HF = "https://hf-mirror.com"
UA = "vt-fetch-ms/1.0"
CHUNK = 1 << 18  # 256 KiB
SPLIT_THRESHOLD = 4 << 20  # 小于 4 MiB 不分段

_lock = threading.Lock()


def log(msg: str, quiet: bool = False) -> None:
    if quiet:
        return
    with _lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()


def build_url(mirror: str, repo: str, path: str, source: str = "ms") -> str:
    """两种通道的 URL 形态完全不同，这里统一收口。

    ms —— ModelScope：`{mirror}/{repo}/repo?Revision=master&FilePath={path}`
    hf —— HuggingFace / hf-mirror：`{mirror}/{repo}/resolve/main/{path}`
          走 HF 通道是因为 **Xenova 的 ONNX 产物只发在 HF**，ModelScope 上
          同名仓库放的是原始 HF 权重（实测：`Xenova/opus-mt-zh-en` 在
          ModelScope 拿到的 config.json 里 `_name_or_path` 指向 Helsinki-NLP）。
    """
    mirror = mirror.rstrip("/")
    if source == "hf":
        return f"{mirror}/{repo}/resolve/main/{urllib.parse.quote(path)}"
    return (
        f"{mirror}/{repo}/repo"
        f"?Revision=master&FilePath={urllib.parse.quote(path)}"
    )


def probe_size(url: str, timeout: int = 30) -> int:
    """用 Range: bytes=0-0 读 Content-Range 拿总大小（ModelScope 的 HEAD 不给 Content-Length）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            return int(cr.rsplit("/", 1)[1])
        n = resp.headers.get("Content-Length")
        if n:
            return int(n)
    raise RuntimeError("无法从响应头得知文件大小（Content-Range / Content-Length 均为空）")


def fetch_range(url: str, start: int, end: int, retries: int = 5) -> bytes:
    """下载 [start, end] 闭区间，失败按指数退避重试。"""
    want = end - start + 1
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                buf = bytearray()
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    buf += chunk
            if len(buf) != want:
                raise IOError(f"短读 {len(buf)} != {want}")
            return bytes(buf)
        except Exception as exc:  # noqa: BLE001 - 网络抖动一律重试
            last = exc
            if attempt == retries:
                break
            time.sleep(min(2**attempt, 15))
    raise RuntimeError(f"区间 [{start},{end}] 下载失败（{retries} 次重试后）: {last}")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def download(url: str, dst: str, segments: int, retries: int, quiet: bool) -> None:
    total = probe_size(url)
    if os.path.exists(dst) and os.path.getsize(dst) == total:
        log(f"  [跳过] {dst} 已存在且大小一致（{total / 1e6:.1f} MB）", quiet)
        return

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    part = dst + ".part"

    seg = 1 if total < SPLIT_THRESHOLD else max(1, min(segments, total // (1 << 20)))
    bounds: list[tuple[int, int]] = []
    step = (total + seg - 1) // seg
    for i in range(seg):
        s = i * step
        e = min(total - 1, s + step - 1)
        if s <= e:
            bounds.append((s, e))

    log(f"  {total / 1e6:.1f} MB 分 {len(bounds)} 段并行下载 …", quiet)
    t0 = time.time()
    done = 0
    with open(part, "wb") as fh:
        fh.truncate(total)
    try:
        with ThreadPoolExecutor(max_workers=len(bounds)) as pool:
            futs = {
                pool.submit(fetch_range, url, s, e, retries): (s, e) for s, e in bounds
            }
            for fut in as_completed(futs):
                s, e = futs[fut]
                data = fut.result()
                with open(part, "r+b") as fh:
                    fh.seek(s)
                    fh.write(data)
                done += len(data)
                dt = time.time() - t0
                log(
                    f"    段 {s:>12,}..{e:<12,} 完成 | "
                    f"{done / 1e6:7.1f}/{total / 1e6:.1f} MB | "
                    f"{done / 1e6 / max(dt, 1e-9):5.2f} MB/s",
                    quiet,
                )
        os.replace(part, dst)
    except Exception:
        if os.path.exists(part):
            os.remove(part)
        raise
    dt = time.time() - t0
    log(f"  [完成] {dst} {total / 1e6:.1f} MB 用时 {dt:.1f}s（均速 {total / 1e6 / max(dt, 1e-9):.2f} MB/s）", quiet)


def main() -> int:
    ap = argparse.ArgumentParser(description="ModelScope 分段并行下载器")
    ap.add_argument("repo", help="如 Xenova/m2m100_418M")
    ap.add_argument("remote_path", help="仓库内路径，如 onnx/encoder_model_int8.onnx")
    ap.add_argument("local_path", help="本地落地路径")
    ap.add_argument("--segments", type=int, default=8)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--sha256", default=None)
    ap.add_argument("--source", choices=("ms", "hf"), default="ms",
                    help="ms=ModelScope（默认，快 22x）；hf=HuggingFace/hf-mirror")
    ap.add_argument("--mirror", default=None, help="覆盖默认镜像地址")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    mirror = args.mirror or (DEFAULT_MIRROR_HF if args.source == "hf" else DEFAULT_MIRROR)
    url = build_url(mirror, args.repo, args.remote_path, args.source)
    log(f"[下载/{args.source}] {args.repo}/{args.remote_path}", args.quiet)
    download(url, args.local_path, args.segments, args.retries, args.quiet)

    if args.sha256:
        got = sha256_of(args.local_path)
        ok = got.lower() == args.sha256.lower()
        log(f"  [SHA256] {'一致' if ok else '不一致'}: {got}", args.quiet)
        if not ok:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
