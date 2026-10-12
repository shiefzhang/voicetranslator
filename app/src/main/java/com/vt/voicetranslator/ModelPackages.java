package com.vt.voicetranslator;

import android.content.Context;
import android.net.Uri;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.*;

final class ModelPackages {
    static final long MAX=3L*1024*1024*1024;
    /** 内置包在 assets 下的目录（由 scripts/model-tiers.py --emit 生成）。 */
    static final String BUNDLED_DIR="bundled-models";
    /**
     * 三类模型包。与 iOS `ModelPackages.Kind`、`scripts/model_pack.py` 的 ENGINES、
     * `scripts/model-tiers.py` 的 KINDS 四处必须对齐。
     *
     * ⚠️ **这一类列表是「契约 + 文档」，不等于「内置了哪几类」** ——
     * 内置集合见 {@link #hasBundled(String)}（v0.2.10 起只有 asr 与 opus）。
     *
     * `opus` 是专用翻译引擎包（OPUS-MT），与 `translation` 是**并列**的两条翻译路径：
     * translation 是通用模型（所有语向的兜底），opus 是「智能选择」档下中英互译与
     * 中日互译走的轻量专用引擎。
     */
    static final String[] KINDS={"asr","translation","opus"};
    /**
     * 导入进度回调。
     *
     * @param message  当前在做什么（给人看的一句话）
     * @param fraction 0…1；**负数表示这一阶段给不出准确比例**，界面该走不确定态，
     *                 而不是假装 0% —— 假装 0% 看起来就是卡住
     */
    interface Progress { void update(String message, double fraction); }

    /**
     * 导入进度收敛器：把「读取压缩包」与「解包校验」两段拼成**一条单调递增**的 0…1。
     *
     * <p>做成收敛器而不是每步各算各的，是为了两件事：
     * <ol>
     *   <li>两段不能各自 0→1，否则进度条会走完一遍再从头走一遍；</li>
     *   <li>逐字节回调有上千次，逐次刷 UI 既浪费又会抖 ——
     *       <b>整数化本身就是节流</b>，一次导入最多 101 次回调。</li>
     * </ol>
     */
    static final class Meter {
        /**
         * 「读取」在整条进度里占的比例。3:7 是经验值：{@code .vtmodel} 是 STORED 的，
         * 「解包」实际做的是搬运 + 逐字节 SHA-256，比单纯读一遍慢。
         */
        private static final double READ_SHARE=0.30;

        private final Progress sink;
        private long readTotal,readDone;      // total 为 0 = 不知道，那一段走不确定态
        private long unpackTotal,unpackDone;
        private int lastPercent=Integer.MIN_VALUE;
        private String lastMessage="";

        Meter(Progress sink){this.sink=sink;}

        /** 读取阶段的总字节；拿不到就传 0。 */
        void beginRead(long total){readTotal=total>0?total:0;readDone=0;}
        void read(long done){readDone=done;emit(false);}
        void beginUnpack(long total){unpackTotal=total>0?total:0;unpackDone=0;}
        void unpack(long done){unpackDone=done;emit(false);}
        void note(String message){if(message.equals(lastMessage))return;lastMessage=message;emit(true);}

        /** 收口：把两段都补满，保证最后一定落在 100%。 */
        void finish(){if(readTotal>0)readDone=readTotal;if(unpackTotal>0)unpackDone=unpackTotal;emit(true);}

        private void emit(boolean force){
            int percent=percent();
            if(!force&&percent==lastPercent)return;
            lastPercent=percent;
            sink.update(lastMessage,percent>=0?percent/100.0:-1);
        }

        private int percent(){
            boolean hasRead=readTotal>0,hasUnpack=unpackTotal>0;
            if(hasRead&&hasUnpack){
                return Math.min(100,(int)Math.round(100*(READ_SHARE*ratio(readDone,readTotal)
                        +(1-READ_SHARE)*ratio(unpackDone,unpackTotal))));
            }
            if(hasUnpack)return (int)Math.round(100*ratio(unpackDone,unpackTotal));
            if(hasRead)return (int)Math.round(100*READ_SHARE*ratio(readDone,readTotal));
            return -1;
        }

        private static double ratio(long done,long total){return total<=0?0:Math.min(1.0,(double)done/total);}
    }
    static boolean safe(String n) {
        if(n.isEmpty()||n.startsWith("/")||n.contains("\\")||n.contains(":"))return false;
        for(String p:n.split("/",-1))if(p.isEmpty()||p.equals(".")||p.equals(".."))return false;
        return true;
    }
    static String hex(byte[] b){StringBuilder s=new StringBuilder();for(byte x:b)s.append(String.format(Locale.ROOT,"%02x",x&255));return s.toString();}
    static void remove(File f)throws IOException {if(f.isDirectory()){File[] children=f.listFiles();if(children!=null)for(File c:children)remove(c);}if(f.exists()&&!f.delete())throw new IOException("清理临时文件失败");}
    static JSONObject readManifest(File d)throws Exception{return new JSONObject(new String(Files.readAllBytes(new File(d,"manifest.json").toPath()),StandardCharsets.UTF_8));}

    /**
     * 🔴 这一类**是否内置**（v0.2.10 起：转写与专用翻译内置，通用翻译不内置）。
     *
     * 为什么 translation 不内置：它是三份里最大的（Q4_K_M 1.04 GiB），
     * 而 Android 的 assets 只能流式读、必须在首启解出来，等于 APK 与内部存储**各占一份**。
     * asr（229 MiB）与 opus（548 MiB）加起来可控，翻译模型交给用户导入。
     *
     * ⚠️ 这张表必须与 `scripts/model-tiers.py` 顶部的 `BUNDLED` 逐项一致，
     * 由 `scripts/tests/test_no_bundled_models.py` 断言（含「translation 不许内置」）。
     */
    static boolean hasBundled(String kind){return "asr".equals(kind)||"opus".equals(kind);}

    /** 内置包在 assets 里的路径；这一类没有内置则为 null。 */
    static String bundledAsset(String kind){return hasBundled(kind)?BUNDLED_DIR+"/"+kind+".vtmodel":null;}

    // ---- 模型包契约（三处一致：本文件 / iOS ModelPackages.swift / scripts/model_pack.py） ----

    /** 该 kind 允许的引擎。 */
    static boolean supportedEngine(String kind,String engine){
        if("asr".equals(kind))return "sherpa-sensevoice".equals(engine);
        if("opus".equals(kind))return "opus-mt-marian".equals(engine);
        return "llama-qwen2".equals(engine)||"llama-gemma3".equals(engine);
    }

    /** manifest 必须声明的文件。 */
    private static String[] requiredFiles(String kind){
        if("asr".equals(kind))return new String[]{"model.int8.onnx","tokens.txt","silero_vad.onnx"};
        // routes.txt 是端侧真正的语向数据源（vt_opus.cc 直接从包根读它）；
        // norm/nfkc.bin 少了的话中文全角标点会整片变 <unk>（见 vt_opus.cc 的 Nfkc）。
        if("opus".equals(kind))return new String[]{"routes.txt","norm/nfkc.bin"};
        return new String[]{"model.gguf"};
    }

    /**
     * 语言契约。
     *
     * asr / translation 必须**恰好**五语（v0.2.4 起，见 scripts/tests/test_model_pack.py）；
     * opus 允许是五语的**非空子集** —— OPUS 是「一个语向一个模型」，默认包只覆盖
     * zh/en/ja，结构上凑不齐五语。但越界（fr/de/ru）一样拒绝：子集不是放水。
     * 契约是**前五语**，不是全部 8 个代码 —— fr/de/ru 只能作翻译目标语言。
     */
    private static boolean languagesOk(String kind,JSONArray declared)throws Exception{
        Set<String> contract=new HashSet<>(Arrays.asList("zh","ja","ko","en","yue"));
        Set<String> seen=new HashSet<>();
        for(int i=0;i<declared.length();i++)seen.add(declared.getString(i));
        if(seen.isEmpty()||!contract.containsAll(seen))return false;
        return "opus".equals(kind)||seen.size()==contract.size();
    }

    // ---- 已安装 / 内置 / 当前生效 ----
    //
    // 三者的关系，**别混用**：
    //   installed = 用户在设置页导入的那一份（内部存储 models/<id>-<uuid>）
    //   bundled   = 首启从 assets 解出来的内置那一份（只有 asr / opus 有）
    //   selected  = installed ?? bundled  ← 「要用某个模型包」的地方只能走这个
    //
    // ⚠️ translation 没有内置可回落，所以 `selected()` 返回 null 是**正常状态**，
    // 不是错误 —— 首次启动就是这个状态，调用方要按「未就绪」处理而不是当异常。
    // asr / opus 在首启 prepareBundled() 之后就有值了。

    /** 用户导入的那一份（内部存储），没有则 null。不含内置。 */
    static File installed(Context c,String kind){
        String p=c.getSharedPreferences("vt",0).getString(kind+"Path","");
        File f=new File(p);
        return !p.isEmpty()&&new File(f,"manifest.json").isFile()?f:null;
    }

    /** 内置那一份已经解出来了（首启 {@link #installBundled} 做的），没有则为 null。 */
    static File bundled(Context c,String kind){
        if(!hasBundled(kind))return null;
        String p=c.getSharedPreferences("vt",0).getString(kind+"Bundled","");
        File f=new File(p);
        return !p.isEmpty()&&new File(f,"manifest.json").isFile()?f:null;
    }

    /**
     * 🔴 **当前实际生效的包：用户导入的优先，没有则回落内置。**
     *
     * 所有「要用某个模型包」的地方都必须走这里，不要直接调 {@link #installed} ——
     * 后者只覆盖用户导入的那一份，内置的会被漏掉（专用引擎会因此永远不生效）。
     */
    static File selected(Context c,String kind){File f=installed(c,kind);return f!=null?f:bundled(c,kind);}

    /** 这一项是否已经有可用的模型包（设置页拿它显示「内置 / 已导入 / 未就绪」）。 */
    static boolean isReady(Context c,String kind){return selected(c,kind)!=null;}

    // ------------------------------------------------------------ 专用引擎覆盖哪些语向

    /**
     * 专用包**实际覆盖**的语向键（{@code zh-en} / {@code en-zh} / {@code zh-ja} …）。
     *
     * <p>🔴 数据源只能是包内的 {@code routes.txt}（一行 {@code 语向<TAB>跳1,跳2}）——
     * 那是端侧 C++ 真正据以装载单包的表（{@code vt_opus.cc} 直接读包根的这个文件）。
     *
     * <p>⚠️ 有两个「看起来像但用不得」的替身：
     * <ul>
     *   <li>设置页那句「中英互译与中日互译」是<b>策略</b>，不是包的能力 —— 用户可以导入
     *       只含 {@code zh-en} 的自制包，策略照样说中译日走专用引擎；</li>
     *   <li>manifest 里的 {@code routes} 字段是<b>跳数</b>（{@code pack-opus-models.py}
     *       写的是 {@code declared_hops}），两跳语向（如 {@code ja-zh} = {@code ja-en}
     *       + {@code en-zh}）在里面根本不出现 —— 拿它当「覆盖了哪些语向」会漏掉整条 pivot。</li>
     * </ul>
     *
     * @return 读不到 routes.txt 时返回 {@code null}（调用方按「不知道」处理，别当「都不覆盖」）
     */
    static Set<String> opusRoutes(File directory){
        File file=new File(directory,"routes.txt");
        if(!file.isFile())return null;
        Set<String> keys=new HashSet<>();
        try{
            for(String raw:Files.readAllLines(file.toPath(),StandardCharsets.UTF_8)){
                String line=raw.trim();
                if(line.isEmpty())continue;
                int tab=line.indexOf('\t');
                keys.add(tab<0?line:line.substring(0,tab));
            }
        }catch(IOException e){
            return null;
        }
        return keys.isEmpty()?null:keys;
    }

    /**
     * 这次会话能不能**只靠专用引擎**跑完 —— 也就是说通用翻译模型包在本次不是必需的。
     *
     * <p>三层判定，缺一不可：
     * <ol>
     *   <li>设置档位与语向（{@link TranslationBackend#prefersOpus}，纯策略）；</li>
     *   <li>有一份可用的专用包（内置或用户导入，走 {@link #selected}）；</li>
     *   <li>这个语向<b>确实在包内的 routes.txt 里</b>。</li>
     * </ol>
     *
     * <p>第 3 层是 v0.2.12 补的。少了它，一个只带 {@code zh-en} 的自制包会让 App
     * 放行到「没有通用模型」的会话里，然后在真正装载时才失败 —— 那时用户已经点过开始了，
     * 看到的是一条语焉不详的错误，而不是「先去导入通用模型包」。
     */
    static boolean opusCovers(Context c,String source,String target,TranslationBackend backend){
        if(!backend.prefersOpus(source,target))return false;
        File directory=selected(c,"opus");
        if(directory==null)return false;
        Set<String> routes=opusRoutes(directory);
        return routes!=null&&routes.contains(source+"-"+target);
    }

    /** 生效的那一份来自哪里；一项都没有则为 null。取值与 iOS `ModelPackages.Source` 对齐。 */
    static String source(Context c,String kind){
        if(installed(c,kind)!=null)return "imported";
        return bundled(c,kind)!=null?"bundled":null;
    }

    /**
     * 当前生效的模型包显示名（还没有时给出待导入的提示）。
     */
    static String displayName(Context c,String kind){
        File d=selected(c,kind);
        if(d!=null){
            String saved=c.getSharedPreferences("vt",0).getString(kind+"Name","");
            if(!saved.isEmpty())return saved;
            try{return readManifest(d).optString("name",d.getName());}catch(Exception e){return d.getName();}
        }
        return "未导入模型包";
    }

    /**
     * 删掉用户导入的那一份。
     *
     * 有内置可回落的两类（asr / opus）删完会**自动回到内置那份**；
     * translation 没有内置，删完就是未就绪、要重新导入。
     *
     * ⚠️ 只删 `kind+"Path"` 记的那一个，不碰 `kind+"Bundled"` —— 内置那份是 App
     * 自己要用的，移掉它等于把功能删了（想省空间应当卸载内置，不是这里）。
     */
    static void removeInstalled(Context c,String kind)throws IOException {
        File base=new File(c.getNoBackupFilesDir(),"models");
        File current=installed(c,kind);
        if(current!=null&&current.getCanonicalPath().startsWith(base.getCanonicalPath()+File.separator))remove(current);
        c.getSharedPreferences("vt",0).edit().remove(kind+"Path").remove(kind+"Name").apply();
    }

    static String engine(File d)throws Exception{return readManifest(d).getString("engine");}

    // ---------------------------------------------------------------- 大包判定（>2 GiB）

    /** EOCD 最短 22 字节；再加注释上限 65535，就是需要扫描的尾巴长度。 */
    private static final int EOCD_MIN=22, EOCD_SCAN=65557;
    private static final long U32_MAX=0xFFFFFFFFL;
    private static final int U16_MAX=0xFFFF;

    /**
     * 快路径（{@link ZipFile}）用起来安全吗？不安全就走 {@link VtStoredZip}。
     *
     * ⚠️ 这里**故意不用「文件超过 2 GiB 就换路径」这种体积猜测**。
     * 会坏的从来不是文件大小，而是**中央目录偏移**：2 GiB（0x80000000）是有符号
     * int32 的悬崖，越过去之后 {@code ZipFile} 会把 EOCD 里的 cdOffset 读成负数，
     * 抛出 `invalid CEN header (bad signature)`。文件大小只是它的一个很粗的近似
     * ——两者差着一个中央目录本身的长度，边界上会判错。
     *
     * 所以直接读包尾的 EOCD，按真实字段判定。判据与 iOS
     * `VTZipArchive.openAtPath:` 完全一致：
     * ① 任一字段顶到哨兵值（0xFFFF / 0xFFFFFFFF）说明真值在 ZIP64 记录里，
     *    32 位字段不可信；② cdOffset + cdSize 越过 int32 上界就会被读成负数。
     */
    private static boolean zipFileIsSafe(File zip){
        try(RandomAccessFile f=new RandomAccessFile(zip,"r")){
            long size=f.length();
            if(size<EOCD_MIN)return false;
            int tailLen=(int)Math.min(size,EOCD_SCAN);
            byte[] tail=new byte[tailLen];
            f.seek(size-tailLen);f.readFully(tail);
            int eocd=-1;
            for(int i=tailLen-EOCD_MIN;i>=0;i--)if(le32(tail,i)==0x06054b50){eocd=i;break;}
            if(eocd<0)return false;                              // 连 EOCD 都找不到 → 交给能报错的那条路
            long entryCount=le16(tail,eocd+10);
            long cdSize=le32(tail,eocd+12);
            long cdOffset=le32(tail,eocd+16);
            if(entryCount==U16_MAX||cdSize==U32_MAX||cdOffset==U32_MAX)return false;  // ZIP64 → 32 位字段不可信
            if(cdOffset>Integer.MAX_VALUE||cdOffset+cdSize>Integer.MAX_VALUE)return false;  // int32 悬崖
            return cdOffset+cdSize<=size;
        }catch(IOException e){
            return false;
        }
    }

    private static int le16(byte[] b,int o){return (b[o]&0xFF)|((b[o+1]&0xFF)<<8);}
    private static long le32(byte[] b,int o){
        return (b[o]&0xFFL)|((b[o+1]&0xFFL)<<8)|((b[o+2]&0xFFL)<<16)|((b[o+3]&0xFFL)<<24);
    }

    /**
     * 大包/兜底路径：从裸流顺序解包，全程不碰中央目录。
     *
     * 走{@link VtStoredZip}而不是 {@code ZipInputStream}：后者对 ZIP64 本地头的
     * 支持随版本而异，失败时是**安静地少读/多读字节**，最后只表现为 SHA-256 失败。
     * 详见 VtStoredZip 的类注释。
     */
    private static File installLargeZip(Context c,File zip,String kind,Meter meter,boolean fromBundled)throws Exception {
        try(InputStream in=new BufferedInputStream(new FileInputStream(zip),1<<20)){return unpack(c,in,kind,meter,fromBundled);}
    }

    /**
     * 从任意 zip 流解包并激活。
     *
     * **用户导入与内置包解包走的是同一个方法**，校验规则（清单字段、路径安全、大小、
     * SHA-256、原子激活）与磁盘快路径 {@link #unpackWithZipFile} 逐条相同 ——
     * 三条路径因此不可能出现分叉。唯一的差别是激活后写哪个 prefs 键：
     * 用户导入写 `kind+"Path"`，内置写 `kind+"Bundled"`。
     *
     * @param fromBundled true = 这是首启解 assets 里的内置包（不是用户导入的）
     */
    private static File unpack(Context c,InputStream raw,String kind,Meter meter,boolean fromBundled)throws Exception {
        String pathKey=kind+(fromBundled?"Bundled":"Path");
        File base=new File(c.getNoBackupFilesDir(),"models");
        // 必须在这里建，而不是靠调用方：干净安装时 models/ 还不存在，
        // 而 staging.mkdir() 不建父目录会直接失败。
        if(!base.isDirectory()&&!base.mkdirs())throw new IOException("无法创建模型目录");
        File staging=new File(base,"stage-"+UUID.randomUUID());
        // 顺序读器：自己解析本地文件头（含 ZIP64），全过程不碰中央目录 —— 详见 VtStoredZip。
        // 大包与不可 seek 的来源（如导入时的裸流）都走它。
        try(VtStoredZip zin=new VtStoredZip(raw)){
            VtStoredZip.Entry manifestEntry=zin.next();
            if(manifestEntry==null||!"manifest.json".equals(manifestEntry.name))throw new IOException("缺少有效 manifest.json，请选择 .vtmodel 模型包");
            if(manifestEntry.size>VtStoredZip.MAX_MANIFEST_BYTES)throw new IOException("清单过大");
            byte[] buf=new byte[1024*1024];
            ByteArrayOutputStream manifestOut=new ByteArrayOutputStream();
            zin.copyTo(manifestOut,buf,manifestEntry.size,null);
            byte[] mb=manifestOut.toByteArray();JSONObject m=new JSONObject(new String(mb,StandardCharsets.UTF_8));
            if(m.getInt("schemaVersion")!=1||!kind.equals(m.getString("kind")))throw new IOException("模型包类型不符");
            if(!supportedEngine(kind,m.getString("engine")))throw new IOException("暂不支持此模型引擎");
            String id=m.getString("id");if(!id.matches("[a-z0-9][a-z0-9._-]{0,79}"))throw new IOException("模型包 ID 无效");
            if(!languagesOk(kind,m.getJSONArray("languages")))throw new IOException("模型包语言不符合契约");
            JSONArray fs=m.getJSONArray("files");Map<String,JSONObject> files=new HashMap<>();long total=0;boolean license=false;for(int i=0;i<fs.length();i++){JSONObject f=fs.getJSONObject(i);String p=f.getString("path");long size=f.getLong("size");if(!safe(p)||p.equals("manifest.json")||files.put(p,f)!=null||size<0||size>MAX||!f.getString("sha256").matches("[a-f0-9]{64}"))throw new IOException("模型清单无效");total+=size;license|=p.startsWith("licenses/");}
            for(String p:requiredFiles(kind))if(!files.containsKey(p))throw new IOException("缺少 "+p);if(!license||total>MAX)throw new IOException("缺少许可或模型过大");if(base.getUsableSpace()<total+64L*1024*1024)throw new IOException("存储空间不足，解包需要 "+(total/1024/1024)+" MB");
            if(!staging.mkdir())throw new IOException("无法创建解包目录");
            // 进度走「已处理的清单字节 / 清单总字节」。用清单声明的大小而不是磁盘实际占用：
            // SHA-256 是逐字节算的，声明多少就得读多少，两者在这里等价。
            meter.beginUnpack(total);
            Set<String> names=new HashSet<>();
            long unpacked=0;
            VtStoredZip.Entry entry;
            while((entry=zin.next())!=null){
                String name=entry.name;
                if(!safe(name)||!names.add(name)||(!files.containsKey(name)))throw new IOException("压缩包存在未声明或危险路径");
                JSONObject spec=files.get(name);long expected=spec.getLong("size");
                // 先按本地头比对长度再搬运：长度不对就没必要白算 2 GB 的 SHA-256。
                if(entry.size!=expected)throw new IOException("文件大小不符："+name+"（包内 "+entry.size+"，清单 "+expected+"）");
                File dest=new File(staging,name);if(!dest.getCanonicalPath().startsWith(staging.getCanonicalPath()+File.separator))throw new IOException("路径越界");
                File parent=dest.getParentFile();if(!parent.isDirectory()&&!parent.mkdirs())throw new IOException("无法创建文件夹");
                MessageDigest hash=MessageDigest.getInstance("SHA-256");
                meter.note("正在校验 "+name+"…");
                final long baseline=unpacked;
                try(OutputStream out=new FileOutputStream(dest)){zin.copyTo(out,buf,entry.size,hash,written->meter.unpack(baseline+written));}
                if(!hex(hash.digest()).equals(spec.getString("sha256")))throw new IOException("校验失败："+name);
                unpacked+=entry.size;
                meter.unpack(unpacked);
            }
            if(names.size()!=files.size())throw new IOException("模型包文件不完整");Files.write(new File(staging,"manifest.json").toPath(),mb);File activated=new File(base,id+"-"+UUID.randomUUID());if(!staging.renameTo(activated))throw new IOException("无法激活模型包");
            // ⚠️ 只清掉**同一来源**的上一份：内置解包不能把用户导入的删掉（反之亦然），
            // 否则首启解内置包会把用户刚导入的包顺手删了。
            File previous=fromBundled?bundled(c,kind):installed(c,kind);
            android.content.SharedPreferences.Editor ed=c.getSharedPreferences("vt",0).edit().putString(pathKey,activated.getAbsolutePath());
            // 名字只记用户导入那一份的：内置的名字直接从 manifest 读，写进 kind+"Name"
            // 会让「导入后又移除」的场景显示成内置包的名字。
            if(!fromBundled)ed.putString(kind+"Name",m.getString("name"));
            ed.commit();
            if(previous!=null&&previous.getCanonicalPath().startsWith(base.getCanonicalPath()+File.separator)){try{remove(previous);}catch(IOException ignored){}}
            meter.finish();
            return activated;
        }catch(Exception e){if(staging.exists())try{remove(staging);}catch(IOException ignored){}throw e;}
    }
    static File install(Context c,Uri uri,String kind,Progress progress)throws Exception {
        File base=new File(c.getNoBackupFilesDir(),"models");if(!base.isDirectory()&&!base.mkdirs())throw new IOException("无法创建模型目录");
        File zip=File.createTempFile("import-",".zip",base),staging=new File(base,"stage-"+UUID.randomUUID());
        Meter meter=new Meter(progress);
        try{
            // 文件提供方不一定给得出大小（部分第三方提供方、云盘占位文件都读不到）
            // —— 读不到就是 0，那一段走不确定态，不用假比例冒充。
            meter.beginRead(sourceSize(c,uri));
            meter.note("正在读取模型包…");
            try(InputStream in=c.getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(zip)){
                if(in==null)throw new IOException("无法打开文件");byte[] b=new byte[1024*1024];long count=0;int n;
                while((n=in.read(b))!=-1){count+=n;if(count>MAX)throw new IOException("模型包超过 3GB");out.write(b,0,n);meter.read(count);}
            }
            // 判定用 EOCD 里的真实字段，不用文件大小近似 —— 理由见 zipFileIsSafe。
            // 于是「包多大」这件事不再参与任何分支：大包一律走 VtStoredZip，完全不碰中央目录。
            if(!zipFileIsSafe(zip))return installLargeZip(c,zip,kind,meter,false);
            try{
                return unpackWithZipFile(c,zip,kind,meter,base,staging,false);
            }catch(ZipException first){
                // 判定读到的字段仍可能与具体实现存在差异（各 Android 版本对 ZIP64 的处理不完全一致），
                // 所以再加一层兜底：顺序读器完全不读中央目录，是这类问题的终点。
                // 两个异常都留着 —— 只报兜底那次的错误会把真正的线索（invalid CEN header）丢掉。
                try{return installLargeZip(c,zip,kind,meter,false);}
                catch(Exception second){second.addSuppressed(first);throw second;}
            }
        }finally{if(zip.exists())zip.delete();if(staging.exists())remove(staging);}
    }

    /** 待导入文件的大小；提供方不给就说 0（那一段进度走不确定态）。 */
    private static long sourceSize(Context c,Uri uri){
        try(android.database.Cursor cursor=c.getContentResolver().query(uri,
                new String[]{android.provider.OpenableColumns.SIZE},null,null,null)){
            if(cursor!=null&&cursor.moveToFirst()){
                int column=cursor.getColumnIndex(android.provider.OpenableColumns.SIZE);
                if(column>=0&&!cursor.isNull(column))return cursor.getLong(column);
            }
        }catch(Exception ignored){}
        return 0;
    }

    /** 内置资产的大小。{@code noCompress 'vtmodel'} 让 assets 原样入包，所以 openFd 拿得到长度。 */
    private static long assetSize(Context c,String asset){
        try{return c.getAssets().openFd(asset).getLength();}catch(Exception ignored){return 0;}
    }

    /**
     * 解出内置包（首启调用一次，见 `MainActivity.prepareBundled()`）。
     *
     * ⚠️ **Android 必须走这一步，iOS 不用**：assets 只能流式读，而模型文件在推理时
     * 要求随机读，所以内置包得先从 assets 解出来再使用。代价是内置包在 APK 与
     * 内部存储**各占一份**（asr + opus ≈ 780 MiB → 约 1.6 GiB）。
     * iOS 的 `BundledModels/<kind>/` 是 bundle 里的普通目录，可直接随机读，
     * 因此那边 `bundled()` 直接返回 bundle 路径、不解包。
     *
     * @return 解好的目录；这一类没有内置、或 assets 里没有对应文件时为 null
     */
    static File installBundled(Context c,String kind,Progress progress)throws Exception {
        if(!hasBundled(kind))return null;
        File existing=bundled(c,kind);
        if(existing!=null)return existing;                  // 已经解过，直接复用
        String asset=bundledAsset(kind);
        File base=new File(c.getNoBackupFilesDir(),"models");
        if(!base.isDirectory()&&!base.mkdirs())throw new IOException("无法创建模型目录");
        // 先落到临时文件再交给 unpack()/unpackWithZipFile()：既统一了校验路径，
        // 也让 zipFileIsSafe() 能按真实 EOCD 字段判定（assets 流本身不可 seek）。
        File temp=File.createTempFile("bundled-",".zip",base);
        File staging=new File(base,"stage-"+UUID.randomUUID());
        Meter meter=new Meter(progress);
        try{
            meter.beginRead(assetSize(c,asset));
            meter.note("正在准备内置"+("asr".equals(kind)?"转写":"专用翻译")+"模型…");
            try(InputStream in=c.getAssets().open(asset);OutputStream out=new FileOutputStream(temp)){
                byte[] b=new byte[1024*1024];int n;long count=0;
                while((n=in.read(b))!=-1){count+=n;if(count>MAX)throw new IOException("内置模型包超过 3GB");out.write(b,0,n);meter.read(count);}
            }
            if(!zipFileIsSafe(temp))return installLargeZip(c,temp,kind,meter,true);
            try{
                return unpackWithZipFile(c,temp,kind,meter,base,staging,true);
            }catch(ZipException first){
                try{return installLargeZip(c,temp,kind,meter,true);}
                catch(Exception second){second.addSuppressed(first);throw second;}
            }
        }finally{
            // 与 install() 一样：临时 zip 与 staging 都要收干净，否则首启失败会
            // 在内部存储留一份 780 MiB 的垃圾。
            if(temp.exists())temp.delete();
            if(staging.exists())try{remove(staging);}catch(IOException ignored){}
        }
    }

    /**
     * {@link ZipFile} 快路径。只在 {@link #zipFileIsSafe} 判定为安全时调用。
     *
     * 它的价值是能拿 {@code ZipEntry.getSize()} 与清单交叉核对，而且对「文件数过多」
     * 这类结构异常报得更早；大包路径没有这些，因此这里不能删。
     */
    private static File unpackWithZipFile(Context c,File zip,String kind,Meter meter,File base,File staging)throws Exception{
        return unpackWithZipFile(c,zip,kind,meter,base,staging,false);
    }

    /**
     * {@link #unpackWithZipFile} 的实现体。
     *
     * @param fromBundled true = 首启解内置包（写 `kind+"Bundled"`），false = 用户导入（写 `kind+"Path"`）
     */
    private static File unpackWithZipFile(Context c,File zip,String kind,Meter meter,File base,File staging,boolean fromBundled)throws Exception{
        String pathKey=kind+(fromBundled?"Bundled":"Path");
        try(ZipFile z=new ZipFile(zip)){
                ZipEntry me=z.getEntry("manifest.json");if(me==null||me.getSize()<0||me.getSize()>131072)throw new IOException("缺少有效 manifest.json，请选择 .vtmodel 模型包");
                byte[] mb;try(InputStream in=z.getInputStream(me);ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] chunk=new byte[4096];int n;while((n=in.read(chunk))!=-1){if(out.size()+n>131072)throw new IOException("清单过大");out.write(chunk,0,n);}mb=out.toByteArray();}
                if(mb.length>131072)throw new IOException("清单过大");JSONObject m=new JSONObject(new String(mb,StandardCharsets.UTF_8));
                if(m.getInt("schemaVersion")!=1||!kind.equals(m.getString("kind")))throw new IOException("模型包类型不符");
                if(!supportedEngine(kind,m.getString("engine")))throw new IOException("暂不支持此模型引擎");
                String id=m.getString("id");if(!id.matches("[a-z0-9][a-z0-9._-]{0,79}"))throw new IOException("模型包 ID 无效");
                if(!languagesOk(kind,m.getJSONArray("languages")))throw new IOException("模型包语言不符合契约");
                JSONArray fs=m.getJSONArray("files");Map<String,JSONObject> files=new HashMap<>();long total=0;
                if(fs.length()>127)throw new IOException("文件过多");
                boolean license=false;
                for(int i=0;i<fs.length();i++){JSONObject f=fs.getJSONObject(i);String p=f.getString("path");long size=f.getLong("size");if(!safe(p)||p.equals("manifest.json")||files.put(p,f)!=null||size<0||size>MAX||!f.getString("sha256").matches("[a-f0-9]{64}"))throw new IOException("模型清单无效");total+=size;license|=p.startsWith("licenses/");}
                for(String p:requiredFiles(kind))if(!files.containsKey(p))throw new IOException("缺少 "+p);
                if(!license||total>MAX)throw new IOException("缺少许可或模型过大");
                if(base.getUsableSpace()<total+64L*1024*1024)throw new IOException("存储空间不足，解包需要 "+(total/1024/1024)+" MB");
                Set<String> names=new HashSet<>();Enumeration<? extends ZipEntry> es=z.entries();while(es.hasMoreElements()){ZipEntry e=es.nextElement();if(!safe(e.getName())||!names.add(e.getName())||(!e.getName().equals("manifest.json")&&!files.containsKey(e.getName())))throw new IOException("压缩包存在未声明或危险路径");}
                if(names.size()!=files.size()+1)throw new IOException("模型包文件不完整");
                if(!staging.mkdir())throw new IOException("无法创建解包目录");
                // 进度走「已处理的清单字节 / 清单总字节」—— 理由与 unpack() 里那句相同。
                meter.beginUnpack(total);
                byte[] b=new byte[1024*1024];
                long unpacked=0;
                for(Map.Entry<String,JSONObject> entry:files.entrySet()){
                    String name=entry.getKey();JSONObject spec=entry.getValue();meter.note("正在校验 "+name+"…");
                    ZipEntry ze=z.getEntry(name);long expected=spec.getLong("size");if(ze.getSize()!=expected)throw new IOException("文件大小不符");
                    File dest=new File(staging,name);if(!dest.getCanonicalPath().startsWith(staging.getCanonicalPath()+File.separator))throw new IOException("路径越界");
                    File parent=dest.getParentFile();if(!parent.isDirectory()&&!parent.mkdirs())throw new IOException("无法创建文件夹");
                    MessageDigest hash=MessageDigest.getInstance("SHA-256");long written=0;
                    final long baseline=unpacked;
                    try(InputStream in=z.getInputStream(ze);OutputStream out=new FileOutputStream(dest)){int n;while((n=in.read(b))!=-1){written+=n;if(written>expected)throw new IOException("解包越界");hash.update(b,0,n);out.write(b,0,n);meter.unpack(baseline+written);}}
                    if(written!=expected||!hex(hash.digest()).equals(spec.getString("sha256")))throw new IOException("校验失败："+name);
                    unpacked+=expected;
                    meter.unpack(unpacked);
                }
                Files.write(new File(staging,"manifest.json").toPath(),mb);
                File activated=new File(base,id+"-"+UUID.randomUUID());if(!staging.renameTo(activated))throw new IOException("无法激活模型包");
                // ⚠️ 只清掉**同一来源**的上一份（理由同 unpack()）。
                File previous=fromBundled?bundled(c,kind):installed(c,kind);
                android.content.SharedPreferences.Editor ed=c.getSharedPreferences("vt",0).edit().putString(pathKey,activated.getAbsolutePath());
                if(!fromBundled)ed.putString(kind+"Name",m.getString("name"));
                ed.commit();
                // No engine is active during import. Keep exactly one installed package per kind.
                if(previous!=null&&previous.getCanonicalPath().startsWith(base.getCanonicalPath()+File.separator)){try{remove(previous);}catch(IOException ignored){}}
                meter.finish();
                return activated;
            }
    }
}
