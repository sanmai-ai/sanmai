// config.example.js
//
// EXAMPLE tenant config. Copy to config.js (per deploy / per branch) and fill
// in real values — config.js is generated at deploy time and served no-store,
// never committed with real credentials.
//
// Two globals are injected on the page BEFORE any ES module loads:
//
//   window.__SANMAI_FIREBASE__  → consumed by shared/firebase-config.js
//   window.APP_CONFIG           → normally produced by shared/resolve-tenant.js
//                                 (set it here only to hardcode/override a
//                                 single-tenant deploy).
//
// All values below are PLACEHOLDERS.

// Firebase web-app config for this tenant's Firebase project.
window.__SANMAI_FIREBASE__ = {
  apiKey: 'REPLACE_WITH_FIREBASE_API_KEY',
  authDomain: 'your-project.firebaseapp.com',
  projectId: 'your-project',
  storageBucket: 'your-project.appspot.com',
  messagingSenderId: '000000000000',
  appId: '1:000000000000:web:0000000000000000000000',
  firestoreDb: '(default)', // named Firestore DB, e.g. "sm-rest"
};

// Optional: hardcode APP_CONFIG for a single-tenant deploy. If omitted,
// call resolveTenant() to derive it from the hostname/path at runtime.
window.APP_CONFIG = {
  apiBase: 'https://api.example.com',
  firestoreDb: '(default)',
  firebaseProject: 'your-project',
  brand: 'default',
  venueId: null,
};
