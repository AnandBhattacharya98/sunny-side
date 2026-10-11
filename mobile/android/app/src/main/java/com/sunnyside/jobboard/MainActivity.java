package com.sunnyside.jobboard;

import android.app.DownloadManager;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.widget.Toast;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // A WebView ignores file downloads unless the app handles them. Calendar invites and
        // the account export are behind the login, so the request carries the session cookie.
        bridge.getWebView().setDownloadListener((url, userAgent, contentDisposition, mimeType, length) -> {
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                String cookies = CookieManager.getInstance().getCookie(url);
                if (cookies != null) req.addRequestHeader("Cookie", cookies);
                req.addRequestHeader("User-Agent", userAgent);
                req.setMimeType(mimeType);
                req.setTitle(name);
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                } else {
                    // Older Android needs a storage permission for the shared Downloads folder.
                    req.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, name);
                }
                DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                dm.enqueue(req);
                Toast.makeText(this, "Downloading " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "Couldn't download " + name, Toast.LENGTH_LONG).show();
            }
        });
    }
}
