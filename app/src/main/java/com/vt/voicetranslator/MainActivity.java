package com.vt.voicetranslator;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.*;
import android.text.*;
import android.text.method.LinkMovementMethod;
import android.text.style.BackgroundColorSpan;
import android.text.style.ClickableSpan;
import android.text.style.ForegroundColorSpan;
import android.util.Log;
import android.view.*;
import android.widget.*;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;

public final class MainActivity extends Activity {
    private static final String CHANGELOG=        "版本 0.2.12\n"+
        "• 「智能选择」档下，专用引擎能翻的语向（中译英、英译中、中译日）不再要求先导入通用翻译模型包，装好即可直接开始翻译\n"+
        "• 是否需要通用翻译模型包，改成按专用包里真实的语向接线判断，而不是按固定的三个语向\n"+
        "• 导入模型包时显示进度条（首启解内置模型包也一样），能看到「读取」与「校验」走到哪里\n"+
        "• 万一专用引擎临时用不上而通用翻译模型包又没导入，会直接说明该导入哪一份，不再报成「加载模型失败」\n\n"+
        "版本 0.2.11\n"+
        "• 修复：语音引擎启动没有响应时，朗读不再把录音一直挂着\n"+
        "• 修复：朗读失败时会说明原因，不再只是安静地没声音\n\n"+
        "版本 0.2.10\n"+
        "• 转写模型包与专用翻译包重新随 App 分发，装好即可用，不必再导入\n"+
        "• 通用翻译模型包仍需自行导入一次（它是三份里最大的，内置会让安装包翻倍）\n"+
        "• 设置页按「内置 / 已导入 / 未就绪」三态显示；自带的显示「内置」\n"+
        "• 导入别的模型包会顶替内置的那一份；点「移除」即回到内置版本（通用翻译除外）\n"+
        "• 注意：从 0.2.9 升级后，通用翻译模型包需要重新导入一次\n\n"+
        "版本 0.2.9\n"+
        "• 模型包不再随 App 分发：转写、翻译、专用翻译三份都由你在设置页导入，各导入一次即可完全离线使用\n"+
        "• 设置页分别显示每份模型包的「已导入 / 未就绪」，缺哪一份会直接说明\n"+
        "• 新增「移除」：可以随时收回模型包占用的空间\n"+
        "• 换模型包更直接：再导入一份就会顶替旧的，不必先移除\n"+
        "• 注意：从旧版本升级后需要重新导入一次模型包\n\n"+
        "版本 0.2.8\n"+
        "• 新增「翻译朗读」：用系统语音读出译文，可在设置里选关闭 / 点按朗读 / 自动朗读\n"+
        "• 译文每行末尾显示喇叭，点哪句读哪句，再点一次停止\n"+
        "• 朗读期间自动暂停录音、读完自动恢复，避免扬声器的声音被麦克风重新采进去\n"+
        "• 自动朗读只读真正的译文，不会把「翻译中…」之类的占位念出来\n\n"+
        "版本 0.2.7\n"+
        "• 设置页新增「翻译引擎」选项：默认档下中英互译与中译日使用更快的轻量专用引擎，其余语向使用通用模型；也可选择全部使用通用模型\n"+
        "• 新增可选的「专用翻译包」，中英互译与中译日不必再逐字生成，通常一秒内出结果\n"+
        "• 专用引擎按需装载：只有本次真正用到的语向才占用内存，启动不再等待\n"+
        "• 没有专用引擎可用时自动回落通用模型，任何语向都照常可用\n\n"+
        "版本 0.2.6\n"+
        "• 转写与翻译模型包已内置到 App，装上即可离线使用，不必再先导入\n"+
        "• 设置页会标明每个模型包是「内置」还是「已导入」\n"+
        "• 导入其他模型包会顶替内置版本，随时可以恢复内置\n"+
        "• 修复「选择模型包」选完文件后没有反应的问题\n\n"+
        "版本 0.2.4\n"+
        "• 新增粤语支持，可选作主语言（SenseVoice 直接识别）或翻译目标语言\n"+
        "• 模型包语言契约由「中日韩英」四语扩展为「中日韩英粤」五语\n\n"+
        "版本 0.2.3\n"+
        "• 翻译进行期间保持屏幕常亮，全部翻译完成后恢复系统息屏设置\n\n"+
        "版本 0.2.2\n"+
        "• 修复 TranslateGemma 大模型导入时的 ZIP 格式兼容问题\n"+
        "• 修正反向不可用时底部导航栏的文字顺序\n\n"+
        "版本 0.2.1\n"+
        "• 转写始终跟随最新内容，翻译继续后台异步处理\n"+
        "• 支持点击原文或译文切换淡黄色对应行\n"+
        "• 错误语种时自动加强指令重试一次\n\n"+
        "版本 0.2.0\n"+
        "• 离线翻译模型可跨录音复用，减少重复加载等待\n"+
        "• 缓存固定翻译提示词，加快连续翻译\n"+
        "• 译文支持生成过程中实时显示\n"+
        "• 处理积压时自动暂停录音，恢复后自动继续\n"+
        "• 设置页增加版本号和版本更新记录\n\n"+
        "版本 0.1.0\n"+
        "• 首次发布\n"+
        "• 支持中文、日语、韩语和英语离线语音翻译\n"+
        "• 支持本地导入转写与翻译模型包\n"+
        "• 支持停顿自动分句和三种界面风格";
    private final String[] codes={"zh","ja","ko","en","yue","fr","de","ru"},names={"中文","日本語","한국어","English","粤语","Français","Deutsch","Русский"},shortNames={"中文","日语","韩语","英语","粤语","法语","德语","俄语"};
    private int page=1,bg,ink,muted,accent,line,surface;
    private SharedPreferences prefs;
    private LinearLayout root,body,nav;
    private TextView sourceView,translatedView,statusView;
    private Button recordButton;
    private ScrollView sourceScroll,translatedScroll;
    private boolean syncingScroll;
    private Pipeline pipeline;
    private boolean importing,destroyed;
    private String importKind="",status="点击开始，停顿后自动翻译";
    /**
     * 模型包导入 / 首启解包的整体进度（0…1，-1 = 这一阶段给不出准确比例）。
     *
     * 与 `status` 分开：`status` 是**文字**（转写与翻译也在用它），这里是**比例**。
     */
    private double prepFraction=-1;
    /** 设置页里那根进度条；当前界面上没有它时是 null（此时只更新 status 文字）。 */
    private ProgressBar prepBar;
    /** 进度条下面那行说明，与 {@link #prepBar} 同生共死。 */
    private TextView prepLabel;
    private static final String SHARE_MODE="shareMode";
    private static final String[] shareModeLabels={"转写内容","翻译内容","转写/翻译对照","先转写后翻译"};
    private final String[] drafts={"",""};
    private final String[] focusedKeys={"",""},renderedFocus={"",""};
    private final boolean[] scrollSelectionRequested={false,false};
    private final ArrayList<LinkedHashMap<String,String[]>> history=new ArrayList<>();
    private long session=0;
    /** 主线程 Handler —— 朗读要「等采集停稳」再出声，需要延时轮询。 */
    private final Handler main=new Handler(Looper.getMainLooper());
    /** 译文朗读。它和录音抢同一条音频通路，所以由本类统一调度（见 startSpeech 附近）。 */
    private Speaker speaker;
    /** 正在朗读哪一行（历史表里的 key）；没在读就是 null。UI 据此把喇叭换成停止标记。 */
    private String speakingKey;
    /** 待播队列。**只保留最新一条**，否则朗读会越追越远、永远慢于说话。 */
    private Speech pendingSpeech;
    /** 这次朗读是否已经让录音把音频通路让出来了。连续朗读时保持，避免来回切。 */
    private boolean speechHoldActive;
    /** 「本机没有××语音」这类提示只弹一次 —— 自动朗读下每句都会撞上同一个原因。 */
    private boolean speechWarningShown;
    /**
     * 首启正在后台解内置模型包（`prepareBundled()`）。
     *
     * 这时点「开始」不该报「缺模型包」—— 那会让人以为包坏了。用一句
     * 「正在准备…请稍候」把这几十秒的窗口说清楚。
     */
    private volatile boolean bundledBusy;
    /** 一条待朗读的译文。 */
    private static final class Speech{final String key,text,language,label;Speech(String k,String t,String lang,String lab){key=k;text=t;language=lang;label=lab;}}
    @Override public void onCreate(Bundle b){super.onCreate(b);prefs=getSharedPreferences("vt",0);history.add(new LinkedHashMap<>());history.add(new LinkedHashMap<>());
        // 朗读的两种结局都必须收口：读完/被停 → 恢复录音；没读成 → 说明原因并恢复录音。
        speaker=new Speaker(this,new Speaker.Listener(){
            public void onFinished(){ui(()->speechFinished());}
            public void onUnavailable(String m){ui(()->speechUnavailable(m));}
        });
        prepareBundled();
        render();}
    private int dp(float n){return (int)(n*getResources().getDisplayMetrics().density+0.5f);}
    private int index(String code){for(int i=0;i<codes.length;i++)if(codes[i].equals(code))return i;return 0;}
    private boolean reverseAvailable(){String target=other();return target.equals("zh")||target.equals("ja")||target.equals("ko")||target.equals("en")||target.equals("yue");}
    private String main(){return prefs.getString("main","zh");}
    private String other(){return prefs.getString("other","en");}
    private String appVersion(){try{return getPackageManager().getPackageInfo(getPackageName(),0).versionName;}catch(PackageManager.NameNotFoundException e){return "未知";}}
    private String src(int p){return p==1?main():other();}
    private String dst(int p){return p==1?other():main();}
    private void palette(){String s=prefs.getString("theme","fresh");boolean dark=s.equals("dark"),warm=s.equals("warm");bg=Color.parseColor(dark?"#171B24":warm?"#FAF5EB":"#FFFFFF");ink=Color.parseColor(dark?"#EDF2FC":warm?"#302A22":"#142346");muted=Color.parseColor(dark?"#AAB6CF":warm?"#80715D":"#7B8CAD");accent=Color.parseColor(dark?"#80ACFF":warm?"#997431":"#1468EF");line=Color.parseColor(dark?"#303849":warm?"#E7DDCD":"#E3EAF3");surface=Color.parseColor(dark?"#29354C":warm?"#F0E4CD":"#E5EFFF");getWindow().setStatusBarColor(bg);getWindow().setNavigationBarColor(bg);getWindow().getDecorView().setSystemUiVisibility(dark?0:View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);}
    private TextView text(String value,float sp,int color){TextView v=new TextView(this);v.setText(value);v.setTextSize(sp);v.setTextColor(color);v.setFontFeatureSettings("kern");return v;}
    private LinearLayout vertical(){LinearLayout l=new LinearLayout(this);l.setOrientation(LinearLayout.VERTICAL);return l;}
    private void divider(LinearLayout parent){View v=new View(this);v.setBackgroundColor(line);parent.addView(v,new LinearLayout.LayoutParams(-1,dp(1)));}
    private GradientDrawable shape(int color,int stroke){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(12));if(stroke!=0)d.setStroke(dp(1),stroke);return d;}
    private Button button(String label,boolean primary){Button b=new Button(this);b.setText(label);b.setAllCaps(false);b.setTextSize(16);b.setTextColor(primary?Color.WHITE:accent);b.setBackground(shape(primary?accent:bg,primary?0:accent));b.setMinHeight(dp(48));b.setElevation(0);b.setStateListAnimator(null);return b;}
    private void render(){
        palette();root=vertical();root.setBackgroundColor(bg);root.setFitsSystemWindows(true);setContentView(root);
        // 旧的那根进度条随上一次 setContentView 一起被丢弃了，清掉引用，
        // 免得进度回调去改一个已经脱离窗口的视图。settings() 会重新挂上。
        prepBar=null;prepLabel=null;
        body=vertical();root.addView(body,new LinearLayout.LayoutParams(-1,0,1));
        if(page==2)settings();else translationPage();
        divider(root);nav=new LinearLayout(this);nav.setPadding(dp(8),dp(5),dp(8),dp(5));root.addView(nav,new LinearLayout.LayoutParams(-1,dp(70)));
        int[] icons={android.R.drawable.ic_menu_revert,android.R.drawable.ic_menu_send,android.R.drawable.ic_menu_preferences};
        String[] labels={reverseAvailable()?"译成"+shortNames[index(main())]:"反向不可用","译成"+shortNames[index(other())],"设置"};
        for(int i=0;i<3;i++){final int p=i;LinearLayout item=vertical();item.setGravity(Gravity.CENTER);item.setContentDescription(labels[i]);item.setClickable(true);ImageView icon=new ImageView(this);icon.setImageResource(icons[i]);icon.setImageTintList(ColorStateList.valueOf(page==i?accent:muted));item.addView(icon,new LinearLayout.LayoutParams(dp(25),dp(25)));TextView title=text(labels[i],12,page==i?accent:muted);title.setGravity(Gravity.CENTER);title.setPadding(0,dp(4),0,0);item.addView(title);nav.addView(item,new LinearLayout.LayoutParams(0,-1,1));item.setOnClickListener(v->{if(p==0&&!reverseAvailable()){message("法语、德语和俄语仅支持作为翻译目标语言，暂不支持反向翻译");return;}if(page!=p){if(pipeline!=null)pipeline.stop();stopSpeaking();page=p;render();}});}
    }
    private void translationPage(){
        LinearLayout header=new LinearLayout(this);header.setGravity(Gravity.CENTER_VERTICAL);header.setPadding(dp(22),dp(14),dp(16),dp(12));
        TextView title=text(names[index(src(page))]+"  →  "+names[index(dst(page))],22,accent);title.setTypeface(null,Typeface.BOLD);header.addView(title,new LinearLayout.LayoutParams(0,-2,1));
        ImageButton more=new ImageButton(this);more.setImageResource(android.R.drawable.ic_menu_more);more.setImageTintList(ColorStateList.valueOf(muted));more.setBackgroundColor(Color.TRANSPARENT);more.setContentDescription("复制或清空文本");header.addView(more,new LinearLayout.LayoutParams(dp(44),dp(44)));more.setOnClickListener(v->textMenu());body.addView(header);
        translatedView=text("",21,ink);sourceView=text("",17,ink);translatedView.setLineSpacing(dp(2),1);sourceView.setLineSpacing(dp(1),1);translatedView.setMovementMethod(LinkMovementMethod.getInstance());sourceView.setMovementMethod(LinkMovementMethod.getInstance());translatedView.setHighlightColor(Color.TRANSPARENT);sourceView.setHighlightColor(Color.TRANSPARENT);
        body.addView(window(translatedView,true),paneParams());body.addView(window(sourceView,false),paneParams());
        translatedScroll.setOnScrollChangeListener((v,x,y,oldX,oldY)->syncScroll(translatedScroll,sourceScroll));
        sourceScroll.setOnScrollChangeListener((v,x,y,oldX,oldY)->syncScroll(sourceScroll,translatedScroll));
        statusView=text(status,13,muted);statusView.setGravity(Gravity.CENTER);statusView.setPadding(dp(12),dp(8),dp(12),dp(10));body.addView(statusView);
        LinearLayout actions=new LinearLayout(this);actions.setPadding(dp(22),dp(4),dp(22),dp(24));actions.setGravity(Gravity.CENTER_VERTICAL);body.addView(actions,new LinearLayout.LayoutParams(-1,dp(80)));
        recordButton=button(pipeline==null?"开始翻译":pipeline.isRecording()?"停止翻译":"处理中…",pipeline==null);recordButton.setTextSize(19);recordButton.setCompoundDrawablesWithIntrinsicBounds(pipeline==null?android.R.drawable.ic_btn_speak_now:android.R.drawable.ic_media_pause,0,0,0);recordButton.setCompoundDrawableTintList(ColorStateList.valueOf(pipeline==null?Color.WHITE:accent));recordButton.setPadding(dp(12),0,dp(12),0);actions.addView(recordButton,new LinearLayout.LayoutParams(0,dp(56),1));
        Button share=button("分享",false);share.setContentDescription("分享转写和翻译文本");LinearLayout.LayoutParams sp=new LinearLayout.LayoutParams(dp(100),dp(56));sp.leftMargin=dp(10);actions.addView(share,sp);share.setOnClickListener(v->shareText());recordButton.setEnabled(!importing);recordButton.setOnClickListener(v->{if(pipeline!=null){pipeline.stop();status="录音已停止，正在处理剩余句子…";update();}else start();});update();
    }
    private LinearLayout.LayoutParams paneParams(){LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,0,1);p.setMargins(dp(14),dp(4),dp(14),dp(6));return p;}
    private LinearLayout window(TextView content,boolean translation){
        LinearLayout card=vertical();card.setBackground(shape(translation?surface:bg,line));
        TextView label=text((translation?"译文 · ":"原文 · ")+names[index(translation?dst(page):src(page))],12,translation?accent:muted);
        label.setTypeface(null,Typeface.BOLD);label.setPadding(dp(16),dp(12),dp(16),dp(6));card.addView(label);
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);scroll.setClipToPadding(false);
        content.setPadding(dp(16),dp(4),dp(16),dp(14));scroll.addView(content);card.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        if(translation)translatedScroll=scroll;else sourceScroll=scroll;return card;
    }
    private int scrollRange(ScrollView view){return Math.max(0,view.getChildAt(0).getHeight()-view.getHeight()+view.getPaddingTop()+view.getPaddingBottom());}
    private void syncScroll(ScrollView from,ScrollView to){if(syncingScroll)return;int range=scrollRange(from);if(range==0)return;syncingScroll=true;try{to.scrollTo(0,Math.round((float)from.getScrollY()/range*scrollRange(to)));}finally{syncingScroll=false;}}
    private void scrollSourceToLatest(){final ScrollView bottom=sourceScroll;bottom.post(()->{if(bottom!=sourceScroll||page==2)return;syncingScroll=true;try{bottom.scrollTo(0,scrollRange(bottom));}finally{syncingScroll=false;}});}
    private String rowText(String value){return value.replace('\r',' ').replace('\n',' ').trim();}
    private void scrollToFocus(int sourceOffset,int translationOffset){final ScrollView top=translatedScroll,bottom=sourceScroll;final TextView topText=translatedView,bottomText=sourceView;top.post(()->{if(top!=translatedScroll||page==2)return;syncingScroll=true;try{scrollToOffset(top,topText,translationOffset);scrollToOffset(bottom,bottomText,sourceOffset);}finally{syncingScroll=false;}});}
    private void scrollTranslationToFocus(int translationOffset){final ScrollView top=translatedScroll;final TextView topText=translatedView;top.post(()->{if(top!=translatedScroll||page==2)return;syncingScroll=true;try{scrollToOffset(top,topText,translationOffset);}finally{syncingScroll=false;}});}
    private void scrollToOffset(ScrollView scroll,TextView content,int offset){Layout layout=content.getLayout();if(layout==null)return;int safe=Math.max(0,Math.min(offset,content.length()));int lineNumber=layout.getLineForOffset(safe);int y=layout.getLineTop(lineNumber)-scroll.getHeight()/2+content.getLineHeight()/2;scroll.scrollTo(0,Math.max(0,y));}
    private ClickableSpan rowSelector(int targetPage,String key){return new ClickableSpan(){public void onClick(View widget){focusedKeys[targetPage]=key;scrollSelectionRequested[targetPage]=true;update();}public void updateDrawState(TextPaint paint){paint.setColor(ink);paint.setUnderlineText(false);}};}
    private void update(){if(page==2||sourceView==null)return;SpannableStringBuilder a=new SpannableStringBuilder(),b=new SpannableStringBuilder();int focusSource=-1,focusTranslation=-1,highlight=Color.rgb(255,244,179);String focus=focusedKeys[page];
        boolean showSpeakers=speechMode().showsSpeaker();
        for(Map.Entry<String,String[]> item:history.get(page).entrySet()){if(a.length()>0)a.append("\n");if(b.length()>0)b.append("\n");int sa=a.length(),sb=b.length();String key=item.getKey();String[] entry=item.getValue();String translation=rowText(entry[1]);a.append(rowText(entry[0]));b.append(translation);int se=b.length();if(a.length()>sa)a.setSpan(rowSelector(page,key),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);if(se>sb)b.setSpan(rowSelector(page,key),sb,se,Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);if(showSpeakers&&se>sb&&!isPlaceholderTranslation(translation)){boolean active=key.equals(speakingKey);b.append("  ");int gs=b.length();b.append(active?"⏹":"🔊");b.setSpan(new ForegroundColorSpan(active?accent:muted),gs,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);b.setSpan(speechToggle(page,key),gs,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}if(key.equals(focus)){focusSource=sa;focusTranslation=sb;if(a.length()>sa){a.setSpan(new BackgroundColorSpan(highlight),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);a.setSpan(new ForegroundColorSpan(Color.rgb(58,45,0)),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}if(b.length()>sb){b.setSpan(new BackgroundColorSpan(highlight),sb,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);b.setSpan(new ForegroundColorSpan(Color.rgb(58,45,0)),sb,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}}}
        if(!drafts[page].isEmpty()){if(a.length()>0)a.append("\n");int start=a.length();a.append(drafts[page]);a.setSpan(new ForegroundColorSpan(muted),start,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}String sourceText=a.length()==0?"转写文本将显示在这里":a.toString(),translationText=b.length()==0?"翻译文本将显示在这里":b.toString();boolean sourceChanged=!sourceText.contentEquals(sourceView.getText()),translationChanged=!translationText.contentEquals(translatedView.getText()),focusChanged=!focus.equals(renderedFocus[page]),userSelection=scrollSelectionRequested[page];renderedFocus[page]=focus;scrollSelectionRequested[page]=false;sourceView.setText(a.length()==0?"转写文本将显示在这里":a);translatedView.setText(b.length()==0?"翻译文本将显示在这里":b);if(userSelection&&focusSource>=0)scrollToFocus(focusSource,focusTranslation);else{if(sourceChanged)scrollSourceToLatest();if(focusTranslation>=0&&(translationChanged||focusChanged))scrollTranslationToFocus(focusTranslation);}statusView.setText(status);if(recordButton!=null)recordButton.setText(pipeline==null?"开始翻译":pipeline.isRecording()?"停止翻译":"处理中…");}
    private void setTranslationScreenAwake(boolean awake){
        if(awake)getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }
    private void start(){
        if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},50);return;}
        // 转写与专用翻译是**内置**的，通用翻译要用户导入 —— 但它**不是每个语向都必需**。
        // v0.2.12：智能选择档下，只要这个语向在专用包的接线里（包内 routes.txt），
        // 专用引擎一个人就能翻完，不该拦着用户去导入那 1 GiB。判定走 opusCovers，
        // 与设置页的提示文案是**同一个**函数 —— 两处各写一套的话，就会重演
        // 「设置页说能用、点开始却报缺包」这类对不上的毛病。
        final TranslationBackend engine=translationBackend();
        final File opus=ModelPackages.selected(this,"opus");
        final boolean translationRequired=!ModelPackages.opusCovers(this,src(page),dst(page),engine);
        File asr=ModelPackages.selected(this,"asr"),mt=ModelPackages.selected(this,"translation");
        if(asr==null||(translationRequired&&mt==null)){
            // 按缺什么报什么：内置解包失败（转写缺了）与「还没导入通用翻译包」要能分开。
            if(bundledBusy){
                message("正在准备内置模型包，请稍候再试");
                return;
            }
            StringBuilder missing=new StringBuilder();
            if(asr==null)missing.append("转写");
            if(translationRequired&&mt==null){if(missing.length()>0)missing.append("、");missing.append("翻译");}
            new AlertDialog.Builder(this).setTitle("先准备模型包").setMessage("还缺"+missing+"模型包。请在设置里点「导入模型包」选择文件，导入后即可完全离线使用。").setPositiveButton("前往设置",(d,w)->{page=2;render();}).setNegativeButton("取消",null).show();
            return;
        }
        if(history.get(page).size()>=200){message("本页已达 200 句，请复制并清空后继续");return;}
        // 专用翻译包是**可选**的：没有它（用户没导入、或自己移除了）就传 null，
        // Pipeline 会按设置档位判定后回落通用模型 —— 不在这里拦。
        // 通用翻译包同理：这次靠专用引擎跑时 mt 就是 null，Pipeline 只在
        // 真正装不上专用引擎时才需要它，那时会给出明确的错误。
        final int targetPage=page;final long id=++session;focusedKeys[targetPage]="";setTranslationScreenAwake(true);
        pipeline=new Pipeline(asr,mt,opus,engine,src(page),dst(page),prefs.getInt("pause",800),new Pipeline.Listener(){
            public void state(String s){ui(()->{status=s;update();});}
            public void draft(String s){ui(()->{drafts[targetPage]=s;update();});}
            public void focus(long n){ui(()->{focusedKeys[targetPage]=id+":"+n;update();});}
            public void sentence(long n,String source,String translation){ui(()->{
                history.get(targetPage).put(id+":"+n,new String[]{source,translation});
                if(history.get(targetPage).size()>=200&&pipeline!=null)pipeline.stop();
                update();
                // 自动朗读：只在**真译文**到达时触发，占位与错误文案跳过
                // （isPlaceholderTranslation 认的就是「翻译中…」「翻译失败：…」这些）。
                String text=rowText(translation);
                if(speechMode().speaksAutomatically()&&!isPlaceholderTranslation(text)){
                    String language=dst(targetPage);
                    requestSpeech(new Speech(id+":"+n,text,language,names[index(language)]),false);
                }
            });}
            public void done(){ui(()->{setTranslationScreenAwake(false);pipeline=null;if(!status.startsWith("错误")&&!status.startsWith("录音错误"))status="录音已停止 · 点击开始新的翻译";render();});}
        });pipeline.start();status="正在准备模型…";render();
    }
    private void ui(Runnable r){runOnUiThread(()->{if(!destroyed)r.run();});}
    /** 进度条上的整数百分比；给不出比例时是 0（那根条此时是不确定态）。 */
    private int prepPercent(){return prepFraction<0?0:(int)Math.round(prepFraction*100);}
    /**
     * 收到一条导入 / 解包进度。
     *
     * <p>刻意**不调 render()**：进度回调最多 101 次，逐次重建整页设置太浪费。
     * 只就地改那根进度条与它下面那行字；界面上没有进度条（当前不在设置页）时，
     * 退而更新译文页的状态栏，让人至少看得见它在动。
     */
    private void showPrep(String message,double fraction){
        status=message;
        prepFraction=fraction;
        if(prepBar!=null){
            barIndeterminate(prepBar,fraction<0);
            prepBar.setProgress(prepPercent());
            if(prepLabel!=null)prepLabel.setText(message);
        }else if(statusView!=null&&page!=2){
            statusView.setText(message);
        }
    }
    /**
     * 切换进度条的「确定 / 不确定」态。
     *
     * <p>`setIndeterminate` 在 Android 13 之后**只允许在附加到窗口前**调用，
     * 之后再调会弹 `IllegalStateException`。这里两种形态各建一次的场景只有
     * settings() 里那一处（创建时就按当前状态定好），运行期只在
     * 「读取总大小拿不到」那种少见情况下才会真的需要切换，
     * 所以包一层 try：切换失败就保持原样，不值得为一条进度反馈把界面搞崩。
     */
    private static void barIndeterminate(ProgressBar bar,boolean indeterminate){
        if(bar.isIndeterminate()==indeterminate)return;
        try{bar.setIndeterminate(indeterminate);}catch(Exception ignored){}
    }
    private void message(String s){new AlertDialog.Builder(this).setMessage(s).setPositiveButton("知道了",null).show();}
    private void settings(){
        TextView title=text("设置",30,ink);title.setTypeface(null,Typeface.BOLD);title.setPadding(dp(24),dp(22),dp(24),dp(22));body.addView(title);divider(body);
        ScrollView scroll=new ScrollView(this);LinearLayout list=vertical();scroll.addView(list);body.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout pause=vertical();pause.setPadding(dp(24),dp(18),dp(24),dp(14));LinearLayout pauseTitle=new LinearLayout(this);pauseTitle.addView(text("停顿时间",17,ink),new LinearLayout.LayoutParams(0,-2,1));TextView pv=text(prefs.getInt("pause",800)/1000f+" 秒",17,accent);pauseTitle.addView(pv);pause.addView(pauseTitle);SeekBar seek=new SeekBar(this);seek.setMax(17);seek.setProgress((prefs.getInt("pause",800)-300)/100);seek.setProgressTintList(ColorStateList.valueOf(accent));seek.setThumbTintList(ColorStateList.valueOf(accent));seek.setContentDescription("停顿时间，0.3 到 2 秒");pause.addView(seek,new LinearLayout.LayoutParams(-1,dp(42)));seek.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onProgressChanged(SeekBar s,int n,boolean user){int ms=300+n*100;pv.setText(ms/1000f+" 秒");if(user)prefs.edit().putInt("pause",ms).apply();}public void onStartTrackingTouch(SeekBar s){}public void onStopTrackingTouch(SeekBar s){}});list.addView(pause);divider(list);
        row(list,"主语言",names[index(main())],()->language("main"));row(list,"翻译语言",names[index(other())],()->language("other"));
        row(list,"翻译引擎",translationBackend().label,this::backend);
        row(list,"翻译朗读",speechMode().label,this::speech);
        row(list,"分享内容",shareModeLabels[prefs.getInt(SHARE_MODE,2)],this::shareMode);
        modelHeader(list);
        modelRow(list,"转写模型包","asr");modelRow(list,"翻译模型包","translation");modelRow(list,"专用翻译包","opus");
        LinearLayout themes=vertical();themes.setPadding(dp(24),dp(18),dp(24),dp(20));themes.addView(text("界面风格",18,ink));LinearLayout choices=new LinearLayout(this);choices.setPadding(0,dp(14),0,0);String[] keys={"dark","fresh","warm"},labels={"深色","清爽","暖色"};int[] colors={Color.rgb(37,43,57),Color.rgb(231,241,255),Color.rgb(252,237,207)};
        for(int i=0;i<3;i++){final String key=keys[i];Button b=button(labels[i],false);b.setTextColor(i==0?Color.WHITE:Color.rgb(35,54,82));b.setBackground(shape(colors[i],prefs.getString("theme","fresh").equals(key)?accent:line));LinearLayout.LayoutParams lp=new LinearLayout.LayoutParams(0,dp(66),1);if(i>0)lp.leftMargin=dp(10);choices.addView(b,lp);b.setOnClickListener(v->{prefs.edit().putString("theme",key).apply();render();});}themes.addView(choices);list.addView(themes);
        // 通用翻译要用户导入 —— 但它**不是每个语向都用得上**（v0.2.12）。
        // 这段话要说清五件事：缺的只有通用翻译、当前语向可能由专用引擎顶着、
        // 专用包没有时会降级、导入会顶替 / 移除会回到内置、朗读用的是系统语音。
        boolean asrReady=ModelPackages.selected(this,"asr")!=null;
        boolean translationReady=ModelPackages.selected(this,"translation")!=null;
        // 与 start() 的门禁走**同一个**判定 —— 两处各写一套就会重演
        // 「设置页说能用、点开始却报缺包」这类对不上的毛病。
        boolean opusCoversNow=ModelPackages.opusCovers(this,main(),other(),translationBackend());
        StringBuilder tipText=new StringBuilder();
        if(!asrReady){
            tipText.append("转写模型包尚未就绪，请稍候或重新导入（转写与专用翻译已内置）");
        }else if(translationReady){
            tipText.append("模型已就绪，可完全离线使用");
            if(ModelPackages.selected(this,"opus")==null)tipText.append("\n没有专用翻译包，翻译引擎会只用通用模型");
        }else if(opusCoversNow){
            tipText.append("当前语向由内置的专用引擎翻译，无需先导入通用翻译模型包");
            tipText.append("\n换成其他语向（或把翻译引擎改成「统一模型」）时才需要导入");
        }else{
            tipText.append("还缺通用翻译模型包，导入后即可完全离线使用（转写与专用翻译已内置）");
        }
        tipText.append("\n导入其他模型包会顶替当前版本；移除导入的版本会回到内置的那一份");
        // 朗读用的是**系统**语音包，不在我们的模型包里 —— 日/韩/粤常常要用户另外下载，
        // 不说清楚就会被当成 bug。
        if(speechMode().showsSpeaker())tipText.append("\n朗读用系统语音（不联网）；日/韩/粤的语音数据可能要另行下载");
        tipText.append("\n对话仅保留本次使用，音频不保存");
        if(importing||bundledBusy){
            // 动辄 1 GiB，光有一行文字看不出「在走」还是「卡死了」。
            // 比例由 ModelPackages.Meter 给出（读取阶段 + 解包校验阶段拼成一条单调递增的 0…1）。
            LinearLayout prep=vertical();prep.setPadding(dp(24),dp(12),dp(24),dp(20));
            ProgressBar bar=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);
            bar.setMax(100);bar.setIndeterminate(prepFraction<0);bar.setProgress(prepPercent());
            bar.setProgressTintList(ColorStateList.valueOf(accent));
            bar.setContentDescription("模型包导入进度");
            prep.addView(bar,new LinearLayout.LayoutParams(-1,dp(10)));
            TextView label=text(status,12,muted);label.setPadding(0,dp(8),0,0);prep.addView(label);
            prepBar=bar;prepLabel=label;list.addView(prep);
        }else{
            prepBar=null;prepLabel=null;
            TextView tip=text(tipText.toString(),12,muted);tip.setPadding(dp(24),0,dp(24),dp(20));list.addView(tip);
        }
        divider(list);LinearLayout about=vertical();about.setGravity(Gravity.CENTER_HORIZONTAL);about.setPadding(dp(24),dp(20),dp(24),dp(28));
        Button changes=button("查看版本更新记录",false);about.addView(changes,new LinearLayout.LayoutParams(-1,dp(48)));changes.setOnClickListener(v->showChangelog());
        TextView version=text("离线语音翻译  ·  版本 "+appVersion(),12,muted);version.setGravity(Gravity.CENTER);version.setPadding(0,dp(14),0,0);about.addView(version);list.addView(about);
    }
    private void showChangelog(){TextView content=text(CHANGELOG,15,ink);content.setLineSpacing(dp(5),1);content.setPadding(dp(24),dp(8),dp(24),dp(12));content.setTextIsSelectable(true);ScrollView scroll=new ScrollView(this);scroll.addView(content);new AlertDialog.Builder(this).setTitle("版本更新记录").setView(scroll).setPositiveButton("知道了",null).show();}
    private void row(LinearLayout l,String title,String value,Runnable click){LinearLayout row=new LinearLayout(this);row.setPadding(dp(24),dp(20),dp(24),dp(20));row.setGravity(Gravity.CENTER_VERTICAL);row.addView(text(title,18,ink),new LinearLayout.LayoutParams(0,-2,1));row.addView(text(value+"  ›",16,muted));row.setOnClickListener(v->click.run());l.addView(row);divider(l);}
    private void language(String key){
        if(pipeline!=null){message("请等待当前翻译结束后切换语言。");return;}
        String current=prefs.getString(key,key.equals("main")?"zh":"en"),opposite=key.equals("main")?other():main();
        String[] choices=key.equals("main")?Arrays.copyOf(names,5):names;
        new AlertDialog.Builder(this).setTitle(key.equals("main")?"主语言":"翻译语言").setSingleChoiceItems(choices,index(current),(d,n)->{
            if(key.equals("main")&&n>=5){Toast.makeText(this,"法语、德语和俄语仅支持作为翻译目标语言",Toast.LENGTH_SHORT).show();return;}
            if(codes[n].equals(opposite)){Toast.makeText(this,"主语言与翻译语言不能相同",Toast.LENGTH_SHORT).show();return;}
            d.dismiss();if(codes[n].equals(current))return;
            Runnable change=()->{stopSpeaking();for(int p=0;p<2;p++){history.get(p).clear();drafts[p]="";focusedKeys[p]="";renderedFocus[p]="";}prefs.edit().putString(key,codes[n]).apply();render();};
            if(history.get(0).isEmpty()&&history.get(1).isEmpty()&&drafts[0].isEmpty()&&drafts[1].isEmpty()){change.run();return;}
            new AlertDialog.Builder(this).setTitle("切换语言前处理文本").setMessage("切换到"+names[n]+"将清空两个翻译页，可先复制全部原文和译文。")
                .setPositiveButton("复制并清空后切换",(dialog,w)->{copyAllText();change.run();})
                .setNeutralButton("清空并切换",(dialog,w)->change.run()).setNegativeButton("放弃",null).show();
        }).setNegativeButton("放弃",null).show();
    }
    private void copyAllText(){StringBuilder all=new StringBuilder();for(int p=0;p<2;p++){
        if(history.get(p).isEmpty()&&drafts[p].isEmpty())continue;
        if(all.length()>0)all.append("\n\n");all.append(names[index(src(p))]).append(" → ").append(names[index(dst(p))]).append("\n");
        for(String[] entry:history.get(p).values())all.append("原文：").append(entry[0]).append("\n译文：").append(entry[1]).append("\n");
        if(!drafts[p].isEmpty())all.append("转写中：").append(drafts[p]).append("\n");
    }android.content.ClipboardManager cm=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);cm.setPrimaryClip(ClipData.newPlainText("全部翻译",all.toString()));Toast.makeText(this,"已复制两个翻译页的文本",Toast.LENGTH_SHORT).show();}
    private void shareMode(){new AlertDialog.Builder(this).setTitle("分享内容").setSingleChoiceItems(shareModeLabels,prefs.getInt(SHARE_MODE,2),(d,n)->{prefs.edit().putInt(SHARE_MODE,n).apply();d.dismiss();render();}).setNegativeButton("取消",null).show();}
    /** 当前翻译引擎档位。存的值认不出时回落默认档 —— 见 {@link TranslationBackend#of(String)}。 */
    private TranslationBackend translationBackend(){return TranslationBackend.of(prefs.getString(TranslationBackend.KEY,TranslationBackend.FALLBACK.value));}
    /**
     * 「翻译引擎」选择器。
     *
     * 刻意**不拦正在进行的会话**（与 {@link #language(String)} 的拦法不同）：引擎是在每次
     * 开始翻译时装载的，改这一项不会打断当前会话，只是本次仍用已装载的那一个。两个选项的
     * 完整说明放进 message 一起显示，省得用户在两档之间来回点着看。
     */
    private void backend(){
        final TranslationBackend[] values=TranslationBackend.values();
        String[] labels=new String[values.length];
        StringBuilder detail=new StringBuilder();
        for(int i=0;i<values.length;i++){
            labels[i]=values[i].label;
            if(detail.length()>0)detail.append("\n\n");
            detail.append(values[i].label).append("：").append(values[i].hint());
        }
        new AlertDialog.Builder(this).setTitle("翻译引擎").setMessage(detail.toString())
            .setSingleChoiceItems(labels,translationBackend().ordinal(),(d,n)->{prefs.edit().putString(TranslationBackend.KEY,values[n].value).apply();d.dismiss();render();})
            .setNegativeButton("取消",null).show();
    }

    // ---------- 译文朗读 ----------

    /** 当前朗读档位。存的值认不出时回落默认档 —— 见 {@link SpeechMode#of(String)}。 */
    private SpeechMode speechMode(){return SpeechMode.of(prefs.getString(SpeechMode.KEY,SpeechMode.FALLBACK.value));}
    /**
     * 「翻译朗读」选择器。与 {@link #backend()} 同一套写法：每一档的完整说明都放进 message，
     * 免得用户在几个选项之间来回点着看。
     */
    private void speech(){
        final SpeechMode[] values=SpeechMode.values();
        String[] labels=new String[values.length];
        StringBuilder detail=new StringBuilder();
        for(int i=0;i<values.length;i++){
            labels[i]=values[i].label;
            if(detail.length()>0)detail.append("\n\n");
            detail.append(values[i].label).append("：").append(values[i].hint());
        }
        new AlertDialog.Builder(this).setTitle("翻译朗读").setMessage(detail.toString())
            .setSingleChoiceItems(labels,speechMode().ordinal(),(d,n)->{
                prefs.edit().putString(SpeechMode.KEY,values[n].value).apply();d.dismiss();
                speechWarningShown=false;
                if(!values[n].showsSpeaker())stopSpeaking();   // 刚关掉就立刻停口，不等下一句
                render();
            })
            .setNegativeButton("取消",null).show();
    }
    /** 点喇叭：正在读这一行就停，否则读它（并打断正在读的别的行）。 */
    private void toggleSpeech(int targetPage,String key){
        if(speaker==null||!speechMode().showsSpeaker())return;
        if(key.equals(speakingKey)){stopSpeaking();return;}
        String[] entry=history.get(targetPage).get(key);
        if(entry==null)return;
        String language=dst(targetPage);
        requestSpeech(new Speech(key,rowText(entry[1]),language,names[index(language)]),true);
    }
    /** 停止朗读；会顺带把录音恢复回去。 */
    private void stopSpeaking(){
        pendingSpeech=null;
        if(speaker!=null&&speaker.isSpeaking()){speaker.stop();return;}   // → onFinished → speechFinished()
        releaseSpeechHold();
        if(speakingKey!=null){speakingKey=null;update();}
    }
    /**
     * 排队朗读。
     *
     * @param interrupt {@code true} = 用户直接点的某一行，立刻打断当前朗读；
     *                  {@code false} = 自动朗读，当前这条读完再说，且<b>只保留最新一条</b>待播。
     */
    private void requestSpeech(Speech request,boolean interrupt){
        if(request==null||request.text.isEmpty()||isPlaceholderTranslation(request.text))return;
        if(speaker!=null&&speaker.isSpeaking()){
            pendingSpeech=request;
            if(interrupt)speaker.stop();   // → onFinished → speechFinished() 接力播这一条
            return;
        }
        startSpeech(request);
    }
    /** 真正开始朗读。先让录音把音频通路交出来，等它**确实停了**再出声。 */
    private void startSpeech(Speech request){
        pendingSpeech=null;
        if(speechHoldActive){beginSpeaking(request);return;}
        boolean needWait=pipeline!=null&&pipeline.holdForPlayback();
        waitForCaptureHeld(request,needWait?25:0,needWait);
    }
    /**
     * 轮询等采集线程把音频通路让出来。
     *
     * <p>{@code AudioRecord.stop()} 是异步生效的，不等就出声会被还没停下的麦克风采进去。
     * 最多等 0.5 秒，超时也照说 —— 不能把用户卡在这里。
     */
    private void waitForCaptureHeld(Speech request,int attempts,boolean needWait){
        if(attempts<=0||pipeline==null||pipeline.isPlaybackHeld()){speechHoldActive=needWait;beginSpeaking(request);return;}
        main.postDelayed(()->waitForCaptureHeld(request,attempts-1,needWait),20);
    }
    private void beginSpeaking(Speech request){
        if(speaker==null)return;
        speakingKey=request.key;
        speaker.speak(request.text,request.language,request.label);
        update();
    }
    /** 一条读完（或被停止）后的收口。**朗读唯一的出口**，录音能不能恢复全看这里。 */
    private void speechFinished(){
        speakingKey=null;
        speechWarningShown=false;          // 成功过一次就把提示重新武装
        Speech next=pendingSpeech;
        if(next!=null){pendingSpeech=null;startSpeech(next);return;}   // 接力：保持 hold，别让通路来回切
        releaseSpeechHold();
        update();
    }
    /**
     * 这一条压根没能开始（引擎没装 / 语种没有语音数据）。
     *
     * <p>提示只弹一次：自动朗读下每一句都会撞上同一个原因，逐句弹窗会刷屏。
     * 一旦成功读过一次（{@link #speechFinished()}）就重新武装。
     */
    private void speechUnavailable(String reason){
        speakingKey=null;
        pendingSpeech=null;
        releaseSpeechHold();
        update();
        if(!speechWarningShown){speechWarningShown=true;Toast.makeText(this,reason,Toast.LENGTH_LONG).show();}
    }
    private void releaseSpeechHold(){
        if(!speechHoldActive)return;
        speechHoldActive=false;
        if(pipeline!=null)pipeline.releasePlaybackHold();
    }
    /** 译文行尾喇叭的点击区。**只覆盖喇叭符号本身**，不覆盖译文正文（正文的点击区是 rowSelector）。 */
    private ClickableSpan speechToggle(int targetPage,String key){
        return new ClickableSpan(){
            public void onClick(View widget){toggleSpeech(targetPage,key);}
            public void updateDrawState(TextPaint paint){paint.setUnderlineText(false);}
        };
    }
    /**
     * 这一行是不是「占位 / 出错」而不是真译文。
     * 自动朗读靠它跳过「翻译中…」「翻译失败：…」这些；这些行尾也不给喇叭 —— 读它没有意义。
     */
    private static boolean isPlaceholderTranslation(String text){
        for(String prefix:new String[]{"翻译中","等待转写","（不足两个字","已取消","翻译失败","转写失败","未识别到有效语音"})if(text.startsWith(prefix))return true;
        return false;
    }
    private String shareTextValue(){StringBuilder out=new StringBuilder();int mode=prefs.getInt(SHARE_MODE,2);for(int p=0;p<2;p++){if(history.get(p).isEmpty()&&drafts[p].isEmpty())continue;if(out.length()>0)out.append("\n\n");out.append(names[index(src(p))]).append(" → ").append(names[index(dst(p))]).append("\n");if(mode==0){for(String[] e:history.get(p).values())out.append(e[0]).append("\n");}else if(mode==1){for(String[] e:history.get(p).values())out.append(e[1]).append("\n");}else if(mode==2){for(String[] e:history.get(p).values())out.append("转写：").append(e[0]).append("\n翻译：").append(e[1]).append("\n");}else{out.append("转写：\n");for(String[] e:history.get(p).values())out.append(e[0]).append("\n");out.append("\n翻译：\n");for(String[] e:history.get(p).values())out.append(e[1]).append("\n");}if(!drafts[p].isEmpty())out.append("\n转写中：").append(drafts[p]).append("\n");}return out.toString();}
    private void shareText(){String value=shareTextValue().trim();if(value.isEmpty()){message("暂无可分享的内容");return;}try{String stamp=new SimpleDateFormat("yyyyMMdd_HHmmss",Locale.US).format(new Date());String name="share_"+stamp+".txt";File file=new File(getCacheDir(),name);try(FileOutputStream stream=new FileOutputStream(file)){stream.write(value.getBytes(StandardCharsets.UTF_8));}Uri uri=Uri.parse("content://"+ShareFileProvider.AUTHORITY+"/"+name);Intent send=new Intent(Intent.ACTION_SEND);send.setType("text/plain");send.putExtra(Intent.EXTRA_STREAM,uri);send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);startActivity(Intent.createChooser(send,"分享翻译文本"));}catch(Exception e){message("分享失败："+e.getMessage());}}
    /** 设置页模型区的小标题。 */
    private void modelHeader(LinearLayout l){
        // 转写与专用翻译内置；通用翻译要导入 —— 但**不是每个语向都必需**（v0.2.12）：
        // 智能选择档下，只要专用包接线里有这个语向，就可以先不导入。
        TextView head=text("转写与专用翻译已内置（装好即可用）；通用翻译模型包只在专用引擎没覆盖的语向上才需要导入",12,muted);
        head.setPadding(dp(24),dp(18),dp(24),dp(6));l.addView(head);
    }
    /**
     * 首启把内置包从 assets 解出来（只有 asr 与 opus，见 {@link ModelPackages#hasBundled}）。
     *
     * ⚠️ **Android 必须走这一步**：assets 只能流式读，而模型文件在推理时要随机读。
     * 代价是内置包在 APK 与内部存储各占一份（asr + opus ≈ 780 MiB）。
     * iOS 不需要 —— 那边 bundle 里的目录本身可随机读。
     *
     * 🔴 **失败绝不能拦启动**：解包失败只意味着「转写 / 专用翻译未就绪」，
     * 用户仍然要能进设置页看状态、导入别的包。崩在启动会连界面都看不到。
     * 所以这里吞掉异常只记日志，并在结束时重画一次界面把状态刷新出来。
     */
    private void prepareBundled(){
        bundledBusy=true;
        new Thread(()->{
            for(String kind:new String[]{"asr","opus"}){
                if(!ModelPackages.hasBundled(kind))continue;
                if(ModelPackages.bundled(this,kind)!=null)continue;      // 已解过
                try{ModelPackages.installBundled(this,kind,(m,f)->ui(()->showPrep(m,f)));}
                catch(Exception e){Log.w("VT","内置 "+kind+" 模型包解包失败（不影响导入别的包）",e);}
            }
            ui(()->{bundledBusy=false;prepFraction=-1;if(!importing)status="点击开始，停顿后自动翻译";render();});
        },"vt-prepare-bundled").start();
    }
    private void modelRow(LinearLayout l,String title,String kind){
        LinearLayout row=new LinearLayout(this);row.setPadding(dp(24),dp(16),dp(24),dp(16));row.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout labels=vertical();labels.addView(text(title,18,ink));
        LinearLayout nameLine=new LinearLayout(this);nameLine.setGravity(Gravity.CENTER_VERTICAL);nameLine.setPadding(0,dp(6),dp(8),0);
        nameLine.addView(text(ModelPackages.displayName(this,kind),12,muted));
        // 三态：内置（装好就有）/ 已导入（用户自己导的）/ 未就绪（翻译模型包首次启动就是这样）。
        // 把「内置」显示成「已导入」会让用户以为那是自己导的，进而去找一个不该存在的「移除」。
        String source=ModelPackages.source(this,kind);
        int badgeColor="imported".equals(source)?accent:muted;
        nameLine.addView(text("· "+("bundled".equals(source)?"内置":source==null?"未就绪":"已导入"),12,badgeColor),new LinearLayout.LayoutParams(-2,-2));
        labels.addView(nameLine);row.addView(labels,new LinearLayout.LayoutParams(0,-2,1));
        // 「移除」只对**用户导入的那一份**有意义：内置那份在 assets 里，移掉它等于把功能删了。
        // 移完转写 / 专用翻译会自动回到内置版本；通用翻译没有内置，移完就是未就绪。
        boolean isImported="imported".equals(source);
        if(isImported){
            boolean restores=ModelPackages.hasBundled(kind);
            Button remove=button("移除",false);LinearLayout.LayoutParams rp=new LinearLayout.LayoutParams(dp(104),dp(48));rp.rightMargin=dp(8);
            row.addView(remove,rp);remove.setEnabled(!importing);
            remove.setOnClickListener(v->{
                if(pipeline!=null){message("请等待录音与翻译结束后再移除模型包");return;}
                try{
                    ModelPackages.removeInstalled(this,kind);render();
                    Toast.makeText(this,restores?"已移除，回到内置模型包":"已移除模型包",Toast.LENGTH_SHORT).show();
                }catch(Exception e){message("移除失败："+e.getMessage());}
            });
        }
        Button pick=button(ModelPackages.selected(this,kind)!=null?"更换":"导入模型包",false);row.addView(pick,new LinearLayout.LayoutParams(dp(124),dp(48)));pick.setEnabled(!importing);
        pick.setOnClickListener(v->{if(pipeline!=null){message("正在处理之前的录音，请完成后再导入模型包。");return;}importKind=kind;Intent in=new Intent(Intent.ACTION_OPEN_DOCUMENT);in.addCategory(Intent.CATEGORY_OPENABLE);in.setType("*/*");startActivityForResult(in,60);});
        l.addView(row);divider(l);
    }
    private void textMenu(){new AlertDialog.Builder(this).setItems(new String[]{"复制本页文本","清空本页"},(d,n)->{if(n==0){android.content.ClipboardManager cm=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);cm.setPrimaryClip(ClipData.newPlainText("翻译",translatedView.getText()+"\n\n"+sourceView.getText()));Toast.makeText(this,"已复制",Toast.LENGTH_SHORT).show();}else if(pipeline!=null)message("请等待录音与翻译结束后清空");else new AlertDialog.Builder(this).setMessage("清空本页所有文本？").setNegativeButton("取消",null).setPositiveButton("清空",(x,w)->{stopSpeaking();history.get(page).clear();drafts[page]="";focusedKeys[page]="";renderedFocus[page]="";update();}).show();}).show();}
    @Override public void onRequestPermissionsResult(int r,String[] p,int[] grant){super.onRequestPermissionsResult(r,p,grant);if(r==50){if(grant.length>0&&grant[0]==PackageManager.PERMISSION_GRANTED&&page!=2)start();else message("需要麦克风权限才能录音，请在系统设置中允许。");}}
    /**
     * 设置页「导入模型包 / 更换」的结果（对应 `startActivityForResult(in,60)`）。
     *
     * 这条回调原本是**缺失**的：文件选择器能打开，但选完文件什么都不会发生 ——
     * 也就是导入功能根本没接上。补上之后，导入的包会顶替当前这一份
     * （同一 kind 只保留一个已安装包，见 {@link ModelPackages#install}）。
     */
    @Override protected void onActivityResult(int requestCode,int resultCode,Intent data){
        super.onActivityResult(requestCode,resultCode,data);
        if(requestCode!=60||resultCode!=RESULT_OK||data==null||data.getData()==null)return;
        final Uri uri=data.getData();final String kind=importKind;
        if(kind.isEmpty())return;
        importing=true;importKind="";status="正在读取模型包…";prepFraction=-1;render();
        new Thread(()->{
            try{
                ModelPackages.install(this,uri,kind,(m,f)->ui(()->showPrep(m,f)));
                ui(()->{importing=false;prepFraction=-1;status="点击开始，停顿后自动翻译";render();Toast.makeText(this,"模型包已导入",Toast.LENGTH_SHORT).show();});
            }catch(Exception e){
                ui(()->{importing=false;prepFraction=-1;status="点击开始，停顿后自动翻译";render();message("导入失败："+e.getMessage());});
            }
        }).start();
    }
    @Override protected void onStop(){super.onStop();if(pipeline!=null)pipeline.stop();stopSpeaking();}
    @Override protected void onDestroy(){destroyed=true;if(pipeline!=null)pipeline.cancel();if(speaker!=null){speaker.release();speaker=null;}main.removeCallbacksAndMessages(null);super.onDestroy();}
    @Override public void onConfigurationChanged(android.content.res.Configuration c){super.onConfigurationChanged(c);render();}
}
