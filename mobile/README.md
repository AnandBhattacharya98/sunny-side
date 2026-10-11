# Sunny Side for Android and iOS

The phone apps are a native shell (built with [Capacitor](https://capacitorjs.com)) around the
Sunny Side website. Every page, sign-in and feature comes from the live site, so when you deploy
the site, app users get the change straight away with no store update.

What the apps add on top of the website:

- An app icon and splash screen (the fried egg from `logo-icon.svg`).
- A friendly "You're offline" page with a Try again button when there's no signal.
- Microphone access for talking to Sunny and the interview quiz.
- Calendar invites and the account export download to the phone (Android).
- Links to job postings and company sites open in the phone's browser; LinkedIn sign-in stays in the app.

The website itself is also installable: on Android Chrome use **Add to Home screen**, on iPhone
Safari use **Share → Add to Home Screen**. That uses the manifest and service worker in `static/`.

## Known limits

- **Google sign-in doesn't work inside the app.** Google blocks sign-in from embedded browsers.
  The app shows a message asking people to use a username and password (or the website).
  Fixing this properly means adding a native Google sign-in plugin later.
- **Downloads on iPhone.** Calendar invites and exports download on Android; on iOS they may do
  nothing inside the app. People can use the website for those.
- **No push notifications yet.** That needs Firebase (Android) and an Apple push key (iOS).

## 1. Point the apps at your site

The apps open https://pm-job-hunter-qfva.onrender.com. To build them for a different copy of the site, set
`SUNNY_SERVER_URL` (it must be `https://`):

```bash
cd mobile
npm install
export SUNNY_SERVER_URL=https://another-copy-of-the-site.com
npx cap sync
```

Run `npx cap sync` again whenever you change `SUNNY_SERVER_URL` or `capacitor.config.ts`.

The app id is `com.sunnyside.jobboard`. If you want a different one (it can't change after you
publish), set `SUNNY_APP_ID` and also change `applicationId` in `android/app/build.gradle` and the
bundle id in Xcode.

## 2. Android

### Get a test APK without installing anything

Every push to `main` that touches `mobile/` builds a debug APK in GitHub Actions
(**Actions → Android app → latest run → Artifacts → sunny-side-debug-apk**). You can also start
it by hand with **Run workflow**.

To build it for a different address, set the repository variable `SUNNY_SERVER_URL` (**Settings →
Secrets and variables → Actions → Variables → New repository variable**).

Unzip the artifact, copy `app-debug.apk` to an Android phone and open it (allow "install unknown
apps" when asked).

### Build on your computer

Install [Android Studio](https://developer.android.com/studio), then:

```bash
cd mobile
npm run android      # syncs and opens the project in Android Studio
```

Press **Run** to start it on an emulator or a plugged-in phone.

### Publish to Google Play

1. Create a [Google Play Console](https://play.google.com/console) account (one-time $25 fee).
2. In Android Studio: **Build → Generate Signed App Bundle / APK → Android App Bundle**, and create
   an upload key. Keep the keystore file and its passwords safe; you need them for every update.
3. In Play Console, create the app, fill in the store listing, privacy policy URL, data-safety
   form and content rating, then upload the `.aab` to **Internal testing** first.
4. Before each update, raise `versionCode` (and `versionName`) in `android/app/build.gradle`.

## 3. iOS

iOS apps can only be built on a Mac with Xcode.

```bash
cd mobile
npm install
npm run ios   # opens Xcode
```

In Xcode, select the **App** target → **Signing & Capabilities**, choose your team, and press
**Run** to start it on a simulator or your iPhone. A free Apple ID is enough to run it on your own
phone for 7 days.

### Publish to the App Store

1. Join the [Apple Developer Program](https://developer.apple.com/programs/) ($99 a year).
2. In [App Store Connect](https://appstoreconnect.apple.com), create the app with the bundle id
   `com.sunnyside.jobboard`.
3. In Xcode: **Product → Archive**, then **Distribute App → App Store Connect**. Test it with
   TestFlight before submitting for review.
4. Raise the version and build number in Xcode for each update.

Apple sometimes rejects apps that are "just a website" (guideline 4.2). Sunny Side's
microphone assistant and interview practice help, but if review pushes back,
the usual next steps are push notifications or native Google sign-in.

## Updating the icon or splash screen

Replace the images in `assets/` (1024×1024 icons, 2732×2732 splash screens) and run:

```bash
npm run assets
```
