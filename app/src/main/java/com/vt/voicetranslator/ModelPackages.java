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
    interface Progress { void update(String message); }
    static boolean safe(String n) {
        if(n.isEmpty()||n.startsWith("/")||n.contains("\\")||n.contains(":"))return false;
        for(String p:n.split("/",-1))if(p.isEmpty()||p.equals(".")||p.equals(".."))return false;
        return true;
    }
    static String hex(byte[] b){StringBuilder s=new StringBuilder();for(byte x:b)s.append(String.format(Locale.ROOT,"%02x",x&255));return s.toString();}
    static void remove(File f)throws IOException {if(f.isDirectory()){File[] children=f.listFiles();if(children!=null)for(File c:children)remove(c);}if(f.exists()&&!f.delete())throw new IOException("清理临时文件失败");}
    static JSONObject readManifest(File d)throws Exception{return new JSONObject(new String(Files.readAllBytes(new File(d,"manifest.json").toPath()),StandardCharsets.UTF_8));}
    static File[] installed(Context c,String kind){File base=new File(c.getNoBackupFilesDir(),"models");File[] all=base.listFiles(f->f.isDirectory()&&new File(f,"manifest.json").isFile());return all==null?new File[0]:all;}
    static File selected(Context c,String kind){String p=c.getSharedPreferences("vt",0).getString(kind+"Path","");File f=new File(p);return !p.isEmpty()&&new File(f,"manifest.json").isFile()?f:null;}
    static String engine(File d)throws Exception{return readManifest(d).getString("engine");}
    private static File installLargeZip(Context c,File zip,String kind,Progress progress)throws Exception {
        File base=new File(c.getNoBackupFilesDir(),"models"),staging=new File(base,"stage-"+UUID.randomUUID());
        try(ZipInputStream zin=new ZipInputStream(new BufferedInputStream(new FileInputStream(zip)))){
            ZipEntry manifestEntry=zin.getNextEntry();
            if(manifestEntry==null||!"manifest.json".equals(manifestEntry.getName()))throw new IOException("缺少有效 manifest.json，请选择 .vtmodel 模型包");
            ByteArrayOutputStream manifestOut=new ByteArrayOutputStream();byte[] buf=new byte[1024*1024];int n;while((n=zin.read(buf))!=-1){if(manifestOut.size()+n>131072)throw new IOException("清单过大");manifestOut.write(buf,0,n);}
            byte[] mb=manifestOut.toByteArray();JSONObject m=new JSONObject(new String(mb,StandardCharsets.UTF_8));
            if(m.getInt("schemaVersion")!=1||!kind.equals(m.getString("kind")))throw new IOException("模型包类型不符");
            String engine=m.getString("engine");boolean supported=kind.equals("asr")?engine.equals("sherpa-sensevoice"):engine.equals("llama-qwen2")||engine.equals("llama-gemma3");if(!supported)throw new IOException("暂不支持此模型引擎");
            String id=m.getString("id");if(!id.matches("[a-z0-9][a-z0-9._-]{0,79}"))throw new IOException("模型包 ID 无效");
            Set<String> languages=new HashSet<>();JSONArray la=m.getJSONArray("languages");for(int i=0;i<la.length();i++)languages.add(la.getString(i));Set<String> required=new HashSet<>(Arrays.asList("zh","ja","ko","en","yue"));if(la.length()!=required.size()||!languages.equals(required))throw new IOException(kind.equals("asr")?"转写模型包必须支持中日韩英粤五语":"翻译模型包必须支持中日韩英粤五语");
            JSONArray fs=m.getJSONArray("files");Map<String,JSONObject> files=new HashMap<>();long total=0;boolean license=false;for(int i=0;i<fs.length();i++){JSONObject f=fs.getJSONObject(i);String p=f.getString("path");long size=f.getLong("size");if(!safe(p)||p.equals("manifest.json")||files.put(p,f)!=null||size<0||size>MAX||!f.getString("sha256").matches("[a-f0-9]{64}"))throw new IOException("模型清单无效");total+=size;license|=p.startsWith("licenses/");}
            String[] req=kind.equals("asr")?new String[]{"model.int8.onnx","tokens.txt","silero_vad.onnx"}:new String[]{"model.gguf"};for(String p:req)if(!files.containsKey(p))throw new IOException("缺少 "+p);if(!license||total>MAX)throw new IOException("缺少许可或模型过大");if(base.getUsableSpace()<total+64L*1024*1024)throw new IOException("存储空间不足，解包需要 "+(total/1024/1024)+" MB");
            if(!staging.mkdir())throw new IOException("无法创建解包目录");Set<String> names=new HashSet<>();
            while((manifestEntry=zin.getNextEntry())!=null){String name=manifestEntry.getName();if(!safe(name)||!names.add(name)||(!files.containsKey(name)))throw new IOException("压缩包存在未声明或危险路径");JSONObject spec=files.get(name);long expected=spec.getLong("size");File dest=new File(staging,name);if(!dest.getCanonicalPath().startsWith(staging.getCanonicalPath()+File.separator))throw new IOException("路径越界");File parent=dest.getParentFile();if(!parent.isDirectory()&&!parent.mkdirs())throw new IOException("无法创建文件夹");MessageDigest hash=MessageDigest.getInstance("SHA-256");long written=0;progress.update("正在校验 "+name+"…");try(OutputStream out=new FileOutputStream(dest)){while((n=zin.read(buf))!=-1){written+=n;if(written>expected)throw new IOException("解包越界");hash.update(buf,0,n);out.write(buf,0,n);}}if(written!=expected||!hex(hash.digest()).equals(spec.getString("sha256")))throw new IOException("校验失败："+name);}
            if(names.size()!=files.size())throw new IOException("模型包文件不完整");Files.write(new File(staging,"manifest.json").toPath(),mb);File installed=new File(base,id+"-"+UUID.randomUUID());if(!staging.renameTo(installed))throw new IOException("无法激活模型包");File previous=selected(c,kind);c.getSharedPreferences("vt",0).edit().putString(kind+"Path",installed.getAbsolutePath()).putString(kind+"Name",m.getString("name")).commit();return installed;
        }
    }
    static File install(Context c,Uri uri,String kind,Progress progress)throws Exception {
        File base=new File(c.getNoBackupFilesDir(),"models");if(!base.isDirectory()&&!base.mkdirs())throw new IOException("无法创建模型目录");
        File zip=File.createTempFile("import-",".zip",base),staging=new File(base,"stage-"+UUID.randomUUID());
        try{
            progress.update("正在读取模型包…");
            try(InputStream in=c.getContentResolver().openInputStream(uri);OutputStream out=new FileOutputStream(zip)){
                if(in==null)throw new IOException("无法打开文件");byte[] b=new byte[1024*1024];long count=0;int n;
                while((n=in.read(b))!=-1){count+=n;if(count>MAX)throw new IOException("模型包超过 3GB");out.write(b,0,n);}
            }
            if(zip.length()>Integer.MAX_VALUE)return installLargeZip(c,zip,kind,progress);
            try(ZipFile z=new ZipFile(zip)){
                ZipEntry me=z.getEntry("manifest.json");if(me==null||me.getSize()<0||me.getSize()>131072)throw new IOException("缺少有效 manifest.json，请选择 .vtmodel 模型包");
                byte[] mb;try(InputStream in=z.getInputStream(me);ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] chunk=new byte[4096];int n;while((n=in.read(chunk))!=-1){if(out.size()+n>131072)throw new IOException("清单过大");out.write(chunk,0,n);}mb=out.toByteArray();}
                if(mb.length>131072)throw new IOException("清单过大");JSONObject m=new JSONObject(new String(mb,StandardCharsets.UTF_8));
                if(m.getInt("schemaVersion")!=1||!kind.equals(m.getString("kind")))throw new IOException("模型包类型不符");
                String engine=m.getString("engine");
                boolean supported=kind.equals("asr")?engine.equals("sherpa-sensevoice"):
                    engine.equals("llama-qwen2")||engine.equals("llama-gemma3");
                if(!supported)throw new IOException("暂不支持此模型引擎");
                String id=m.getString("id");if(!id.matches("[a-z0-9][a-z0-9._-]{0,79}"))throw new IOException("模型包 ID 无效");
                Set<String> languages=new HashSet<>();JSONArray la=m.getJSONArray("languages");for(int i=0;i<la.length();i++)languages.add(la.getString(i));
                Set<String> required=new HashSet<>(Arrays.asList("zh","ja","ko","en","yue"));if(la.length()!=required.size()||!languages.equals(required))throw new IOException(kind.equals("asr")?"转写模型包必须支持中日韩英粤五语":"翻译模型包必须支持中日韩英粤五语");
                JSONArray fs=m.getJSONArray("files");Map<String,JSONObject> files=new HashMap<>();long total=0;
                if(fs.length()>127)throw new IOException("文件过多");
                boolean license=false;
                for(int i=0;i<fs.length();i++){JSONObject f=fs.getJSONObject(i);String p=f.getString("path");long size=f.getLong("size");if(!safe(p)||p.equals("manifest.json")||files.put(p,f)!=null||size<0||size>MAX||!f.getString("sha256").matches("[a-f0-9]{64}"))throw new IOException("模型清单无效");total+=size;license|=p.startsWith("licenses/");}
                String[] req=kind.equals("asr")?new String[]{"model.int8.onnx","tokens.txt","silero_vad.onnx"}:new String[]{"model.gguf"};
                for(String p:req)if(!files.containsKey(p))throw new IOException("缺少 "+p);
                if(!license||total>MAX)throw new IOException("缺少许可或模型过大");
                if(base.getUsableSpace()<total+64L*1024*1024)throw new IOException("存储空间不足，解包需要 "+(total/1024/1024)+" MB");
                Set<String> names=new HashSet<>();Enumeration<? extends ZipEntry> es=z.entries();while(es.hasMoreElements()){ZipEntry e=es.nextElement();if(!safe(e.getName())||!names.add(e.getName())||(!e.getName().equals("manifest.json")&&!files.containsKey(e.getName())))throw new IOException("压缩包存在未声明或危险路径");}
                if(names.size()!=files.size()+1)throw new IOException("模型包文件不完整");
                if(!staging.mkdir())throw new IOException("无法创建解包目录");
                byte[] b=new byte[1024*1024];
                for(Map.Entry<String,JSONObject> entry:files.entrySet()){
                    String name=entry.getKey();JSONObject spec=entry.getValue();progress.update("正在校验 "+name+"…");
                    ZipEntry ze=z.getEntry(name);long expected=spec.getLong("size");if(ze.getSize()!=expected)throw new IOException("文件大小不符");
                    File dest=new File(staging,name);if(!dest.getCanonicalPath().startsWith(staging.getCanonicalPath()+File.separator))throw new IOException("路径越界");
                    File parent=dest.getParentFile();if(!parent.isDirectory()&&!parent.mkdirs())throw new IOException("无法创建文件夹");
                    MessageDigest hash=MessageDigest.getInstance("SHA-256");long written=0;
                    try(InputStream in=z.getInputStream(ze);OutputStream out=new FileOutputStream(dest)){int n;while((n=in.read(b))!=-1){written+=n;if(written>expected)throw new IOException("解包越界");hash.update(b,0,n);out.write(b,0,n);}}
                    if(written!=expected||!hex(hash.digest()).equals(spec.getString("sha256")))throw new IOException("校验失败："+name);
                }
                Files.write(new File(staging,"manifest.json").toPath(),mb);
                File installed=new File(base,id+"-"+UUID.randomUUID());if(!staging.renameTo(installed))throw new IOException("无法激活模型包");
                File previous=selected(c,kind);
                c.getSharedPreferences("vt",0).edit().putString(kind+"Path",installed.getAbsolutePath()).putString(kind+"Name",m.getString("name")).commit();
                // No engine is active during import. Keep exactly one installed package per kind.
                if(previous!=null&&previous.getCanonicalPath().startsWith(base.getCanonicalPath()+File.separator)){try{remove(previous);}catch(IOException ignored){}}
                return installed;
            }
        }finally{if(zip.exists())zip.delete();if(staging.exists())remove(staging);}
    }
}
