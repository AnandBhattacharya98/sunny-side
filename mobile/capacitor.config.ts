import type { CapacitorConfig } from '@capacitor/cli';
import { writeFileSync } from 'fs';
import { join } from 'path';

// The apps are a native shell around the Sunny Side website: every page, sign-in and
// update comes from the site, so a deploy reaches app users without a store release.
// SUNNY_SERVER_URL points a build at another copy of the site (e.g. a staging deploy).
const DEFAULT_URL = 'https://pm-job-hunter-qfva.onrender.com';
const serverUrl = (process.env.SUNNY_SERVER_URL || DEFAULT_URL).replace(/\/+$/, '');

// The offline page is a local file, so it needs the site's address for "Try again".
// Capacitor reads this config before copying www/ into the apps, so the file is current.
writeFileSync(join(__dirname, 'www', 'server.js'), `window.SUNNY_SERVER_URL = ${JSON.stringify(serverUrl)};\n`);

const config: CapacitorConfig = {
  appId: process.env.SUNNY_APP_ID || 'com.sunnyside.jobboard',
  appName: 'Sunny Side',
  webDir: 'www',
  // The server checks for this to explain that Google sign-in needs the website.
  appendUserAgent: 'SunnySideApp',
  backgroundColor: '#FAF6EE',
  server: {
    url: serverUrl,
    // Shown when the site can't be reached (no signal, site down).
    errorPath: 'offline.html',
    // LinkedIn sign-in must stay inside the app so the login cookie lands here.
    // Every other outside link (job postings, company sites) opens in the phone's browser.
    allowNavigation: ['www.linkedin.com', '*.linkedin.com'],
  },
  android: {
    allowMixedContent: false,
  },
  ios: {
    contentInset: 'always',
    limitsNavigationsToAppBoundDomains: false,
  },
  plugins: {
    SplashScreen: {
      launchShowDuration: 1200,
      launchAutoHide: true,
      backgroundColor: '#FAF6EE',
      showSpinner: false,
    },
    SystemBars: {
      insetsHandling: 'css',
    },
  },
};

export default config;
