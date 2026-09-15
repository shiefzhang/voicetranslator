package com.vt.voicetranslator;

import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;

public final class ShareFileProvider extends android.content.ContentProvider {
    static final String AUTHORITY = "com.vt.voicetranslator.share";
    @Override public boolean onCreate(){return true;}
    @Override public String getType(Uri uri){return "text/plain";}
    @Override public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException{
        File file=shareFile(uri);
        return ParcelFileDescriptor.open(file,ParcelFileDescriptor.MODE_READ_ONLY);
    }
    @Override public Cursor query(Uri uri,String[] projection,String selection,String[] args,String sort){
        MatrixCursor result=new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME,OpenableColumns.SIZE});
        try{File file=shareFile(uri);result.addRow(new Object[]{file.getName(),file.length()});}catch(FileNotFoundException ignored){}
        return result;
    }
    private File shareFile(Uri uri)throws FileNotFoundException{
        String name=uri.getLastPathSegment();
        if(name==null||!name.matches("share_[0-9]{8}_[0-9]{6}\\.txt"))throw new FileNotFoundException("invalid share file");
        File file=new File(getContext().getCacheDir(),name);
        if(!file.isFile())throw new FileNotFoundException(name);
        return file;
    }
    @Override public int delete(Uri uri,String selection,String[] args){return 0;}
    @Override public int update(Uri uri,android.content.ContentValues values,String selection,String[] args){return 0;}
    @Override public Uri insert(Uri uri,android.content.ContentValues values){return null;}
}
