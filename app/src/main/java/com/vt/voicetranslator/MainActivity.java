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
import android.speech.tts.*;
import android.text.*;
import android.text.method.LinkMovementMethod;
import android.text.style.BackgroundColorSpan;
import android.text.style.ClickableSpan;
import android.text.style.ForegroundColorSpan;
import android.view.*;
import android.widget.*;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;

public final class MainActivity extends Activity {
    private static final String CHANGELOG="版本 0.3.0\n"+
        "• 新增系统 TTS 翻译后朗读开关\n"+
        "• TTS 播放期间暂停语音输入，播放结束后自动恢复\n\n"+
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
    private final String[] codes={"zh","ja","ko","en","fr","de","ru"},names={"中文","日本語","한국어","English","Français","Deutsch","Русский"},shortNames={"中文","日语","韩语","英语","法语","德语","俄语"};
    private int page=1,bg,ink,muted,accent,line,surface;
    private SharedPreferences prefs;
    private LinearLayout root,body,nav;
    private TextView sourceView,translatedView,statusView;
    private Button recordButton;
    private ScrollView sourceScroll,translatedScroll;
    private boolean syncingScroll;
    private Pipeline pipeline;
    private TextToSpeech tts;
    private boolean ttsReady;
    private static final String TTS_ENABLED="ttsEnabled";
    private boolean importing,destroyed;
    private String importKind="",status="点击开始，停顿后自动翻译";
    private static final String SHARE_MODE="shareMode";
    private static final String[] shareModeLabels={"转写内容","翻译内容","转写/翻译对照","先转写后翻译"};
    private final String[] drafts={"",""};
    private final String[] focusedKeys={"",""},renderedFocus={"",""};
    private final boolean[] scrollSelectionRequested={false,false};
    private final ArrayList<LinkedHashMap<String,String[]>> history=new ArrayList<>();
    private long session=0;
    @Override public void onCreate(Bundle b){super.onCreate(b);prefs=getSharedPreferences("vt",0);history.add(new LinkedHashMap<>());history.add(new LinkedHashMap<>());tts=new TextToSpeech(this,status->{ttsReady=status==TextToSpeech.SUCCESS;});render();}
    private int dp(float n){return (int)(n*getResources().getDisplayMetrics().density+0.5f);}
    private int index(String code){for(int i=0;i<codes.length;i++)if(codes[i].equals(code))return i;return 0;}
    private boolean reverseAvailable(){String target=other();return target.equals("zh")||target.equals("ja")||target.equals("ko")||target.equals("en");}
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
        body=vertical();root.addView(body,new LinearLayout.LayoutParams(-1,0,1));
        if(page==2)settings();else translationPage();
        divider(root);nav=new LinearLayout(this);nav.setPadding(dp(8),dp(5),dp(8),dp(5));root.addView(nav,new LinearLayout.LayoutParams(-1,dp(70)));
        int[] icons={android.R.drawable.ic_menu_revert,android.R.drawable.ic_menu_send,android.R.drawable.ic_menu_preferences};
        String[] labels={reverseAvailable()?"译成"+shortNames[index(main())]:"反向不可用","译成"+shortNames[index(other())],"设置"};
        for(int i=0;i<3;i++){final int p=i;LinearLayout item=vertical();item.setGravity(Gravity.CENTER);item.setContentDescription(labels[i]);item.setClickable(true);ImageView icon=new ImageView(this);icon.setImageResource(icons[i]);icon.setImageTintList(ColorStateList.valueOf(page==i?accent:muted));item.addView(icon,new LinearLayout.LayoutParams(dp(25),dp(25)));TextView title=text(labels[i],12,page==i?accent:muted);title.setGravity(Gravity.CENTER);title.setPadding(0,dp(4),0,0);item.addView(title);nav.addView(item,new LinearLayout.LayoutParams(0,-1,1));item.setOnClickListener(v->{if(p==0&&!reverseAvailable()){message("法语、德语和俄语仅支持作为翻译目标语言，暂不支持反向翻译");return;}if(page!=p){if(pipeline!=null)pipeline.stop();page=p;render();}});}
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
    private ClickableSpan rowSelector(int targetPage,String key,String spoken){return new ClickableSpan(){public void onClick(View widget){focusedKeys[targetPage]=key;scrollSelectionRequested[targetPage]=true;speak(spoken,targetPage);update();}public void updateDrawState(TextPaint paint){paint.setColor(ink);paint.setUnderlineText(false);}};}
    private void update(){if(page==2||sourceView==null)return;SpannableStringBuilder a=new SpannableStringBuilder(),b=new SpannableStringBuilder();int focusSource=-1,focusTranslation=-1,highlight=Color.rgb(255,244,179);String focus=focusedKeys[page];
        for(Map.Entry<String,String[]> item:history.get(page).entrySet()){if(a.length()>0)a.append("\n");if(b.length()>0)b.append("\n");int sa=a.length(),sb=b.length();String key=item.getKey();String[] entry=item.getValue();a.append(rowText(entry[0]));b.append(rowText(entry[1]));if(a.length()>sa)a.setSpan(rowSelector(page,key,entry[0]),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);if(b.length()>sb)b.setSpan(rowSelector(page,key,entry[1]),sb,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);if(key.equals(focus)){focusSource=sa;focusTranslation=sb;if(a.length()>sa){a.setSpan(new BackgroundColorSpan(highlight),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);a.setSpan(new ForegroundColorSpan(Color.rgb(58,45,0)),sa,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}if(b.length()>sb){b.setSpan(new BackgroundColorSpan(highlight),sb,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);b.setSpan(new ForegroundColorSpan(Color.rgb(58,45,0)),sb,b.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}}}
        if(!drafts[page].isEmpty()){if(a.length()>0)a.append("\n");int start=a.length();a.append(drafts[page]);a.setSpan(new ForegroundColorSpan(muted),start,a.length(),Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);}String sourceText=a.length()==0?"转写文本将显示在这里":a.toString(),translationText=b.length()==0?"翻译文本将显示在这里":b.toString();boolean sourceChanged=!sourceText.contentEquals(sourceView.getText()),translationChanged=!translationText.contentEquals(translatedView.getText()),focusChanged=!focus.equals(renderedFocus[page]),userSelection=scrollSelectionRequested[page];renderedFocus[page]=focus;scrollSelectionRequested[page]=false;sourceView.setText(a.length()==0?"转写文本将显示在这里":a);translatedView.setText(b.length()==0?"翻译文本将显示在这里":b);if(userSelection&&focusSource>=0)scrollToFocus(focusSource,focusTranslation);else{if(sourceChanged)scrollSourceToLatest();if(focusTranslation>=0&&(translationChanged||focusChanged))scrollTranslationToFocus(focusTranslation);}statusView.setText(status);if(recordButton!=null)recordButton.setText(pipeline==null?"开始翻译":pipeline.isRecording()?"停止翻译":"处理中…");}
    private void speak(String text,int targetPage){if(tts==null){tts=new TextToSpeech(this,status->{ttsReady=status==TextToSpeech.SUCCESS;});}if(!ttsReady){new Handler(Looper.getMainLooper()).postDelayed(()->speak(text,targetPage),700);return;}Locale locale=dst(targetPage).equals("fr")?Locale.FRENCH:dst(targetPage).equals("de")?Locale.GERMAN:dst(targetPage).equals("ru")?new Locale("ru"):dst(targetPage).equals("ja")?Locale.JAPANESE:dst(targetPage).equals("ko")?Locale.KOREAN:dst(targetPage).equals("zh")?Locale.SIMPLIFIED_CHINESE:Locale.US;int lang=tts.setLanguage(locale);if(lang==TextToSpeech.LANG_MISSING_DATA||lang==TextToSpeech.LANG_NOT_SUPPORTED){Toast.makeText(this,"系统未安装"+locale.getDisplayLanguage(Locale.CHINA)+"语音数据",Toast.LENGTH_SHORT).show();if(pipeline!=null)pipeline.resumeInput();return;}tts.setOnUtteranceProgressListener(new UtteranceProgressListener(){public void onStart(String id){}public void onDone(String id){ui(()->{if(pipeline!=null)pipeline.resumeInput();});}public void onError(String id){ui(()->{if(pipeline!=null)pipeline.resumeInput();});}});tts.speak(text,TextToSpeech.QUEUE_FLUSH,null,"vt-"+System.nanoTime());}
    private void setTranslationScreenAwake(boolean awake){
        if(awake)getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }
    private void start(){
        if(checkSelfPermission(Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO},50);return;}
        File asr=ModelPackages.selected(this,"asr"),mt=ModelPackages.selected(this,"translation");if(asr==null||mt==null){new AlertDialog.Builder(this).setTitle("先准备模型包").setMessage("请在设置中导入转写模型包和翻译模型包，随后可完全离线使用。").setPositiveButton("前往设置",(d,w)->{page=2;render();}).setNegativeButton("取消",null).show();return;}
        if(history.get(page).size()>=200){message("本页已达 200 句，请复制并清空后继续");return;}
        final int targetPage=page;final long id=++session;focusedKeys[targetPage]="";setTranslationScreenAwake(true);
        pipeline=new Pipeline(asr,mt,src(page),dst(page),prefs.getInt("pause",800),new Pipeline.Listener(){
            public void state(String s){ui(()->{status=s;update();});}
            public void draft(String s){ui(()->{drafts[targetPage]=s;update();});}
            public void focus(long n){ui(()->{focusedKeys[targetPage]=id+":"+n;update();});}
            public void sentence(long n,String source,String translation){ui(()->{history.get(targetPage).put(id+":"+n,new String[]{source,translation});if(prefs.getBoolean(TTS_ENABLED,false)&&!translation.endsWith("▌")&&!translation.startsWith("翻译中")&&!translation.startsWith("等待")&&!translation.startsWith("翻译失败")&&!translation.equals("已取消")&&pipeline!=null){pipeline.pauseInput();speak(translation,targetPage);}if(history.get(targetPage).size()>=200&&pipeline!=null)pipeline.stop();update();});}
            public void done(){ui(()->{setTranslationScreenAwake(false);pipeline=null;if(!status.startsWith("错误")&&!status.startsWith("录音错误"))status="录音已停止 · 点击开始新的翻译";render();});}
        });pipeline.start();status="正在准备模型…";render();
    }
    private void ui(Runnable r){runOnUiThread(()->{if(!destroyed)r.run();});}
    private void message(String s){new AlertDialog.Builder(this).setMessage(s).setPositiveButton("知道了",null).show();}
    private void settings(){
        TextView title=text("设置",30,ink);title.setTypeface(null,Typeface.BOLD);title.setPadding(dp(24),dp(22),dp(24),dp(22));body.addView(title);divider(body);
        ScrollView scroll=new ScrollView(this);LinearLayout list=vertical();scroll.addView(list);body.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout pause=vertical();pause.setPadding(dp(24),dp(18),dp(24),dp(14));LinearLayout pauseTitle=new LinearLayout(this);pauseTitle.addView(text("停顿时间",17,ink),new LinearLayout.LayoutParams(0,-2,1));TextView pv=text(prefs.getInt("pause",800)/1000f+" 秒",17,accent);pauseTitle.addView(pv);pause.addView(pauseTitle);SeekBar seek=new SeekBar(this);seek.setMax(17);seek.setProgress((prefs.getInt("pause",800)-300)/100);seek.setProgressTintList(ColorStateList.valueOf(accent));seek.setThumbTintList(ColorStateList.valueOf(accent));seek.setContentDescription("停顿时间，0.3 到 2 秒");pause.addView(seek,new LinearLayout.LayoutParams(-1,dp(42)));seek.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener(){public void onProgressChanged(SeekBar s,int n,boolean user){int ms=300+n*100;pv.setText(ms/1000f+" 秒");if(user)prefs.edit().putInt("pause",ms).apply();}public void onStartTrackingTouch(SeekBar s){}public void onStopTrackingTouch(SeekBar s){}});list.addView(pause);divider(list);
        row(list,"主语言",names[index(main())],()->language("main"));row(list,"翻译语言",names[index(other())],()->language("other"));row(list,"分享内容",shareModeLabels[prefs.getInt(SHARE_MODE,2)],this::shareMode);row(list,"翻译后朗读",prefs.getBoolean(TTS_ENABLED,false)?"开启":"关闭",()->{prefs.edit().putBoolean(TTS_ENABLED,!prefs.getBoolean(TTS_ENABLED,false)).apply();render();});
        modelRow(list,"转写模型包","asr");modelRow(list,"翻译模型包","translation");
        LinearLayout themes=vertical();themes.setPadding(dp(24),dp(18),dp(24),dp(20));themes.addView(text("界面风格",18,ink));LinearLayout choices=new LinearLayout(this);choices.setPadding(0,dp(14),0,0);String[] keys={"dark","fresh","warm"},labels={"深色","清爽","暖色"};int[] colors={Color.rgb(37,43,57),Color.rgb(231,241,255),Color.rgb(252,237,207)};
        for(int i=0;i<3;i++){final String key=keys[i];Button b=button(labels[i],false);b.setTextColor(i==0?Color.WHITE:Color.rgb(35,54,82));b.setBackground(shape(colors[i],prefs.getString("theme","fresh").equals(key)?accent:line));LinearLayout.LayoutParams lp=new LinearLayout.LayoutParams(0,dp(66),1);if(i>0)lp.leftMargin=dp(10);choices.addView(b,lp);b.setOnClickListener(v->{prefs.edit().putString("theme",key).apply();render();});}themes.addView(choices);list.addView(themes);
        TextView tip=text(importing?status:"模型从本地文件导入 · 支持中日韩英\n对话仅保留本次使用，音频不保存",12,muted);tip.setPadding(dp(24),0,dp(24),dp(20));list.addView(tip);
        divider(list);LinearLayout about=vertical();about.setGravity(Gravity.CENTER_HORIZONTAL);about.setPadding(dp(24),dp(20),dp(24),dp(28));
        Button changes=button("查看版本更新记录",false);about.addView(changes,new LinearLayout.LayoutParams(-1,dp(48)));changes.setOnClickListener(v->showChangelog());
        TextView version=text("离线语音翻译  ·  版本 "+appVersion(),12,muted);version.setGravity(Gravity.CENTER);version.setPadding(0,dp(14),0,0);about.addView(version);list.addView(about);
    }
    private void showChangelog(){TextView content=text(CHANGELOG,15,ink);content.setLineSpacing(dp(5),1);content.setPadding(dp(24),dp(8),dp(24),dp(12));content.setTextIsSelectable(true);ScrollView scroll=new ScrollView(this);scroll.addView(content);new AlertDialog.Builder(this).setTitle("版本更新记录").setView(scroll).setPositiveButton("知道了",null).show();}
    private void row(LinearLayout l,String title,String value,Runnable click){LinearLayout row=new LinearLayout(this);row.setPadding(dp(24),dp(20),dp(24),dp(20));row.setGravity(Gravity.CENTER_VERTICAL);row.addView(text(title,18,ink),new LinearLayout.LayoutParams(0,-2,1));row.addView(text(value+"  ›",16,muted));row.setOnClickListener(v->click.run());l.addView(row);divider(l);}
    private void language(String key){
        if(pipeline!=null){message("请等待当前翻译结束后切换语言。");return;}
        String current=prefs.getString(key,key.equals("main")?"zh":"en"),opposite=key.equals("main")?other():main();
        String[] choices=key.equals("main")?Arrays.copyOf(names,4):names;
        new AlertDialog.Builder(this).setTitle(key.equals("main")?"主语言":"翻译语言").setSingleChoiceItems(choices,index(current),(d,n)->{
            if(key.equals("main")&&n>=4){Toast.makeText(this,"法语、德语和俄语仅支持作为翻译目标语言",Toast.LENGTH_SHORT).show();return;}
            if(codes[n].equals(opposite)){Toast.makeText(this,"主语言与翻译语言不能相同",Toast.LENGTH_SHORT).show();return;}
            d.dismiss();if(codes[n].equals(current))return;
            Runnable change=()->{for(int p=0;p<2;p++){history.get(p).clear();drafts[p]="";focusedKeys[p]="";renderedFocus[p]="";}prefs.edit().putString(key,codes[n]).apply();render();};
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
    private String shareTextValue(){StringBuilder out=new StringBuilder();int mode=prefs.getInt(SHARE_MODE,2);for(int p=0;p<2;p++){if(history.get(p).isEmpty()&&drafts[p].isEmpty())continue;if(out.length()>0)out.append("\n\n");out.append(names[index(src(p))]).append(" → ").append(names[index(dst(p))]).append("\n");if(mode==0){for(String[] e:history.get(p).values())out.append(e[0]).append("\n");}else if(mode==1){for(String[] e:history.get(p).values())out.append(e[1]).append("\n");}else if(mode==2){for(String[] e:history.get(p).values())out.append("转写：").append(e[0]).append("\n翻译：").append(e[1]).append("\n");}else{out.append("转写：\n");for(String[] e:history.get(p).values())out.append(e[0]).append("\n");out.append("\n翻译：\n");for(String[] e:history.get(p).values())out.append(e[1]).append("\n");}if(!drafts[p].isEmpty())out.append("\n转写中：").append(drafts[p]).append("\n");}return out.toString();}
    private void shareText(){String value=shareTextValue().trim();if(value.isEmpty()){message("暂无可分享的内容");return;}try{String stamp=new SimpleDateFormat("yyyyMMdd_HHmmss",Locale.US).format(new Date());String name="share_"+stamp+".txt";File file=new File(getCacheDir(),name);try(FileOutputStream stream=new FileOutputStream(file)){stream.write(value.getBytes(StandardCharsets.UTF_8));}Uri uri=Uri.parse("content://"+ShareFileProvider.AUTHORITY+"/"+name);Intent send=new Intent(Intent.ACTION_SEND);send.setType("text/plain");send.putExtra(Intent.EXTRA_STREAM,uri);send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);startActivity(Intent.createChooser(send,"分享翻译文本"));}catch(Exception e){message("分享失败："+e.getMessage());}}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request!=60)return;if(result!=RESULT_OK||data==null){message("未选择模型包，请在文件选择器中点击“打开”确认");return;}Uri uri=data.getData();if(uri==null&&data.getClipData()!=null&&data.getClipData().getItemCount()>0)uri=data.getClipData().getItemAt(0).getUri();if(uri==null){message("文件选择器没有返回文件 URI，请改用系统文件管理器重试");return;}final Uri selectedUri=uri;String kind=importKind;importing=true;ProgressDialog dialog=new ProgressDialog(this);dialog.setTitle("导入模型包");dialog.setMessage("正在读取…");dialog.setCancelable(false);dialog.show();new Thread(()->{try{if(kind.equals("translation"))LocalLlm.close();ModelPackages.install(this,selectedUri,kind,s->ui(()->dialog.setMessage(s)));ui(()->{importing=false;dialog.dismiss();status="模型包已导入";render();});}catch(Exception e){ui(()->{importing=false;dialog.dismiss();render();message("导入失败："+e.getMessage());});}},"vt-import").start();}
    private void chooseInstalled(String kind){File[] models=ModelPackages.installed(this,kind);if(models.length<2){message("暂无可快速切换的其他已安装模型");return;}String[] labels=new String[models.length];for(int i=0;i<models.length;i++){try{labels[i]=ModelPackages.readManifest(models[i]).getString("name");}catch(Exception e){labels[i]=models[i].getName();}}new AlertDialog.Builder(this).setTitle("快速切换"+ (kind.equals("translation")?"翻译":"转写")+"模型").setItems(labels,(d,n)->{File f=models[n];try{JSONObject m=ModelPackages.readManifest(f);prefs.edit().putString(kind+"Path",f.getAbsolutePath()).putString(kind+"Name",m.getString("name")).apply();render();}catch(Exception e){message("模型读取失败："+e.getMessage());}}).setNegativeButton("取消",null).show();}
    private void modelRow(LinearLayout l,String title,String kind){LinearLayout row=new LinearLayout(this);row.setPadding(dp(24),dp(16),dp(24),dp(16));row.setGravity(Gravity.CENTER_VERTICAL);LinearLayout labels=vertical();labels.addView(text(title,18,ink));TextView name=text(prefs.getString(kind+"Name","未导入模型包")+"  (点击切换)",12,muted);name.setOnClickListener(v->chooseInstalled(kind));name.setPadding(0,dp(6),dp(8),0);labels.addView(name);row.addView(labels,new LinearLayout.LayoutParams(0,-2,1));Button pick=button("选择模型包",false);row.addView(pick,new LinearLayout.LayoutParams(dp(124),dp(48)));pick.setEnabled(!importing);pick.setOnClickListener(v->{if(pipeline!=null){message("正在处理之前的录音，请完成后再导入模型包。");return;}importKind=kind;Intent in=new Intent(Intent.ACTION_OPEN_DOCUMENT);in.addCategory(Intent.CATEGORY_OPENABLE);in.setType("*/*");startActivityForResult(in,60);});l.addView(row);divider(l);}
    private void textMenu(){new AlertDialog.Builder(this).setItems(new String[]{"复制本页文本","清空本页"},(d,n)->{if(n==0){android.content.ClipboardManager cm=(android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);cm.setPrimaryClip(ClipData.newPlainText("翻译",translatedView.getText()+"\n\n"+sourceView.getText()));Toast.makeText(this,"已复制",Toast.LENGTH_SHORT).show();}else if(pipeline!=null)message("请等待录音与翻译结束后清空");else new AlertDialog.Builder(this).setMessage("清空本页所有文本？").setNegativeButton("取消",null).setPositiveButton("清空",(x,w)->{history.get(page).clear();drafts[page]="";focusedKeys[page]="";renderedFocus[page]="";update();}).show();}).show();}
    @Override public void onRequestPermissionsResult(int r,String[] p,int[] grant){super.onRequestPermissionsResult(r,p,grant);if(r==50){if(grant.length>0&&grant[0]==PackageManager.PERMISSION_GRANTED&&page!=2)start();else message("需要麦克风权限才能录音，请在系统设置中允许。");}}
    @Override protected void onStop(){super.onStop();if(pipeline!=null)pipeline.stop();}
    @Override protected void onDestroy(){destroyed=true;if(pipeline!=null)pipeline.cancel();super.onDestroy();}
    @Override public void onConfigurationChanged(android.content.res.Configuration c){super.onConfigurationChanged(c);render();}
}
