package com.vt.voicetranslator;

import java.io.BufferedInputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * 顺序读取 `.vtmodel`：只吃 STORED 条目，自己解析**本地文件头**（含 ZIP64 扩展字段）。
 *
 * <h2>为什么不用 {@code java.util.zip.ZipInputStream}</h2>
 *
 * `.vtmodel` 是 STORED（无压缩）的，包多大模型就多大 —— TranslateGemma 4B 这一档
 * 必然超过 2 GiB。打包侧（`scripts/model_pack.py`）为了让 Android 能读，
 * **每个条目都写 ZIP64**：本地头里的 size / csize 是 `0xFFFFFFFF`，真值在 id 为
 * `0x0001` 的扩展字段里。`ZipInputStream` 对这条路的支持随 Android 版本而异，
 * 而且失败形态极难诊断 —— 它不会报「ZIP64 没解析」，而是**安静地少读或多读字节**，
 * 最后表现为 SHA-256 校验失败，报错点离真正的原因很远。
 *
 * <h2>为什么不用中央目录</h2>
 *
 * 2 GiB（`0x80000000`）是**中央目录偏移的 32 位有符号悬崖**：
 * `java.util.zip.ZipFile` 会把 EOCD 里的 `cdOffset` 读成负数，直接抛
 * `invalid CEN header (bad signature)`。所以这个读器**完全不碰中央目录** ——
 * 靠「STORED 条目精确拷贝 N 字节后正好落在下一个本地头」这一点顺序推进，
 * 因此对包大小没有任何上限。iOS 侧 `VTZipArchive` 走的也是同一套判据，
 * 只是它用随机访问（`NSFileHandle` 可 seek），这里用顺序推进。
 *
 * <h2>契约（不满足就抛出带原因和修法的异常，不猜）</h2>
 *
 * <ul>
 *   <li>`method` 必须是 0（STORED）—— STORED 下「解压」就是纯字节搬运，没有 inflate，
 *       所以不存在窗口/缓冲边界问题；</li>
 *   <li>`flags` 不能带 bit 3（数据描述符）—— 带它时本地头里的大小是 0，真值在数据之后，
 *       顺序读法必须先 seek 回去补写，而 assets 流不可 seek。</li>
 * </ul>
 *
 * 两者 `scripts/model_pack.py` 的产物都满足（它固定用 `ZIP_STORED` 且写入可 seek 的文件，
 * 因此不会设 bit 3）。
 */
final class VtStoredZip implements Closeable {
    private static final int LOCAL_SIG = 0x04034b50;
    private static final long U32_MAX = 0xFFFFFFFFL;
    private static final int ZIP64_EXTRA_ID = 0x0001;
    private static final int HEADER_BYTES = 30;
    /** 清单上限 128 KiB —— 与 model_pack.py 的 131072 对齐。 */
    static final int MAX_MANIFEST_BYTES = 131072;

    private final InputStream in;
    private final byte[] header = new byte[HEADER_BYTES];

    VtStoredZip(InputStream source) {
        this.in = (source instanceof BufferedInputStream)
                ? source
                : new BufferedInputStream(source, 1 << 20);
    }

    /** 一个条目：名字 + 长度（ZIP64 已解析成 64 位）。 */
    static final class Entry {
        final String name;
        final long size;
        final boolean zip64;

        Entry(String name, long size, boolean zip64) {
            this.name = name;
            this.size = size;
            this.zip64 = zip64;
        }
    }

    /**
     * 读下一个条目；下一个不是本地文件头（即已到中央目录或流尾）时返回 {@code null}。
     *
     * @return 下一个条目，或 {@code null} 表示正常结束
     */
    Entry next() throws IOException {
        if (!readFully(header, 0, 4)) return null;
        if (le32(header, 0) != LOCAL_SIG) return null;  // PK\x01\x02 / PK\x05\x06 …
        fillFully(header, 4, HEADER_BYTES - 4);

        int flags = le16(header, 6);
        int method = le16(header, 8);
        long compressedSize = le32(header, 18);
        long size = le32(header, 22);
        int nameLength = le16(header, 26);
        int extraLength = le16(header, 28);

        if ((flags & 0x0008) != 0) {
            throw new IOException("模型包条目使用了数据描述符（flags bit 3），"
                    + "本地头里没有真实大小；请用 scripts/model_pack.py 重打这个包");
        }
        if (method != 0) {
            throw new IOException("模型包条目使用了压缩（method=" + method
                    + "）；.vtmodel 契约要求 STORED，请用 scripts/model_pack.py 重打这个包");
        }

        byte[] nameBuf = new byte[nameLength];
        fillFully(nameBuf, 0, nameLength);
        String name = new String(nameBuf, StandardCharsets.UTF_8);

        byte[] extra = new byte[extraLength];
        fillFully(extra, 0, extraLength);

        boolean zip64 = false;
        if (size == U32_MAX || compressedSize == U32_MAX) {
            zip64 = true;
            // ZIP64 扩展字段：只补齐那些被写成 0xFFFFFFFF 的字段，顺序固定
            // （uncompressed 在前、compressed 在后，各自只在对应 32 位字段是哨兵时才出现）。
            int p = 0;
            boolean resolved = false;
            while (p + 4 <= extra.length) {
                int id = le16(extra, p);
                int len = le16(extra, p + 2);
                if (p + 4 + len > extra.length) break;
                if (id == ZIP64_EXTRA_ID) {
                    int q = p + 4;
                    int end = q + len;
                    if (size == U32_MAX) {
                        if (q + 8 > end) throw new IOException(name + ": ZIP64 字段不完整");
                        size = le64(extra, q);
                        q += 8;
                    }
                    if (compressedSize == U32_MAX) {
                        if (q + 8 > end) throw new IOException(name + ": ZIP64 字段不完整");
                        compressedSize = le64(extra, q);
                        q += 8;
                    }
                    resolved = true;
                }
                p += 4 + len;
            }
            if (!resolved || size == U32_MAX || compressedSize == U32_MAX) {
                throw new IOException(name + ": 大小未知（本地头是 0xFFFFFFFF 但缺 ZIP64 扩展字段）");
            }
        }
        if (size != compressedSize) {
            throw new IOException(name + ": STORED 条目 size != csize（" + size + " vs " + compressedSize + "）");
        }
        if (size < 0) throw new IOException(name + ": 解出的长度为负");
        return new Entry(name, size, zip64);
    }

    /**
     * 精确搬运 {@code size} 字节。STORED 下这就是「解压」的全部。
     *
     * @param hash 非 null 时同步喂给 SHA-256
     * @return 实际写入的字节数（等于 {@code size}；不足即抛断流异常）
     */
    long copyTo(OutputStream out, byte[] buffer, long size, MessageDigest hash) throws IOException {
        return copyTo(out, buffer, size, hash, null);
    }

    /**
     * 同上，另带一条「已搬运多少字节」的回调。
     *
     * <p>回调**只服务于 UI 进度**，不参与任何判定：大小仍由 {@code size} 卡，
     * 完整性仍由 {@code hash} 卡，回调缺失或乱序都不会放行一个坏包。
     *
     * <p>为什么必须有它：通用翻译包里的 {@code model.gguf} 是单文件 1 GiB 量级，
     * 只有「文件级」粒度的话，这一步会长时间停在同一格，看起来和卡死没区别。
     */
    long copyTo(OutputStream out, byte[] buffer, long size, MessageDigest hash, ByteSink sink)
            throws IOException {
        long remaining = size;
        while (remaining > 0) {
            int want = (int) Math.min(remaining, buffer.length);
            int n = in.read(buffer, 0, want);
            if (n < 0) throw new IOException("模型包被截断：还差 " + remaining + " 字节");
            remaining -= n;
            if (hash != null) hash.update(buffer, 0, n);
            out.write(buffer, 0, n);
            if (sink != null) sink.written(size - remaining);
        }
        return size;
    }

    /** 搬运过程中的字节回调。见 {@link #copyTo(OutputStream, byte[], long, MessageDigest, ByteSink)}。 */
    interface ByteSink {
        /** @param written 本条目**已**搬运的字节数（从 0 单调增到条目大小） */
        void written(long written);
    }

    @Override
    public void close() throws IOException {
        in.close();
    }

    // ---------------------------------------------------------------- 小工具

    /** 尽力读满；到 EOF 时返回 false（不抛），便于「干净的结尾」判定。 */
    private boolean readFully(byte[] buf, int off, int len) throws IOException {
        int got = 0;
        while (got < len) {
            int n = in.read(buf, off + got, len - got);
            if (n < 0) return false;
            got += n;
        }
        return true;
    }

    /** 必须读满：读不满说明包被截断，直接抛。 */
    private void fillFully(byte[] buf, int off, int len) throws IOException {
        if (!readFully(buf, off, len)) {
            throw new IOException("模型包被截断（本地文件头/条目名不完整）");
        }
    }

    private static int le16(byte[] b, int o) {
        return (b[o] & 0xFF) | ((b[o + 1] & 0xFF) << 8);
    }

    private static long le32(byte[] b, int o) {
        return (b[o] & 0xFFL) | ((b[o + 1] & 0xFFL) << 8)
                | ((b[o + 2] & 0xFFL) << 16) | ((b[o + 3] & 0xFFL) << 24);
    }

    private static long le64(byte[] b, int o) {
        return le32(b, o) | (le32(b, o + 4) << 32);
    }
}
