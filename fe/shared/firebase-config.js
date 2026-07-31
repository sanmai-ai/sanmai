// shared/firebase-config.js
//
// Reads the Firebase web-app config that MUST be injected onto the page as
// `window.__SANMAI_FIREBASE__` (e.g. via a per-tenant config.js written at
// deploy time). There is intentionally NO hardcoded project here: a missing
// or malformed injection fails loud so we never silently boot against the
// wrong Firebase project.
//
// Expected injected shape (standard Firebase web config):
//   window.__SANMAI_FIREBASE__ = {
//     apiKey, authDomain, projectId,
//     storageBucket?, messagingSenderId?, appId?, measurementId?,
//     firestoreDb?            // named Firestore DB, e.g. "sm-rest"
//   };

/** @typedef {Object} FirebaseConfig
 * @property {string} apiKey
 * @property {string} authDomain
 * @property {string} projectId
 * @property {string} [storageBucket]
 * @property {string} [messagingSenderId]
 * @property {string} [appId]
 * @property {string} [measurementId]
 * @property {string} [firestoreDb]
 */

const REQUIRED_KEYS = ['apiKey', 'authDomain', 'projectId'];

/**
 * Return the injected Firebase config, validating required keys.
 * @returns {FirebaseConfig}
 * @throws {Error} if window.__SANMAI_FIREBASE__ is absent or incomplete.
 */
export function getFirebaseConfig() {
  const cfg = typeof window !== 'undefined' ? window.__SANMAI_FIREBASE__ : undefined;

  if (!cfg || typeof cfg !== 'object') {
    throw new Error(
      'firebase-config: window.__SANMAI_FIREBASE__ is not set. ' +
        'Inject the tenant Firebase config (config.js) before loading modules.'
    );
  }

  const missing = REQUIRED_KEYS.filter((k) => !cfg[k]);
  if (missing.length) {
    throw new Error(
      `firebase-config: window.__SANMAI_FIREBASE__ missing required key(s): ${missing.join(', ')}`
    );
  }

  return cfg;
}
