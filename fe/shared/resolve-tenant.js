// shared/resolve-tenant.js
//
// Resolves which tenant/venue the current page belongs to, purely from the
// browser location, and publishes the result to `window.APP_CONFIG`.
//
// Resolution inputs (no network):
//   - window.location.hostname        → picks the tenant registry entry
//   - an optional "/v/<venueId>/" path → selects a venue within the tenant
//
// This is deliberately behavior-neutral for the foundation: a single-entry
// registry (the "default" tenant) is used as a catch-all. Real deployments
// extend TENANT_REGISTRY (or replace it with a generated file) without
// changing callers.

/** @typedef {Object} AppConfig
 * @property {string}  apiBase          Base URL of the backend API.
 * @property {string}  firestoreDb      Named Firestore database.
 * @property {string}  firebaseProject  Firebase / GCP project id.
 * @property {string}  brand            Brand key → theme folder under /theme/.
 * @property {string|null} venueId      Selected venue id, or null.
 */

/** @typedef {Object} TenantEntry
 * @property {string} apiBase
 * @property {string} firestoreDb
 * @property {string} firebaseProject
 * @property {string} brand
 */

// Single-entry registry for now. Key is a hostname matcher:
//   "*" is the catch-all fallback.
/** @type {Record<string, TenantEntry>} */
export const TENANT_REGISTRY = {
  '*': {
    apiBase: '/api',
    firestoreDb: '(default)',
    firebaseProject: 'sanmai-foundation',
    brand: 'default',
  },
};

/**
 * Pick a registry entry for a hostname. Exact match wins, else the "*"
 * catch-all. Throws if there is no catch-all and no exact match.
 * @param {string} hostname
 * @returns {TenantEntry}
 */
export function lookupTenant(hostname) {
  const entry = TENANT_REGISTRY[hostname] || TENANT_REGISTRY['*'];
  if (!entry) {
    throw new Error(`resolve-tenant: no registry entry for host "${hostname}" and no "*" fallback`);
  }
  return entry;
}

/**
 * Extract a venue id from a "/v/<venueId>/..." path prefix.
 * @param {string} pathname
 * @returns {string|null}
 */
export function parseVenueFromPath(pathname) {
  const m = /^\/v\/([^/]+)(?:\/|$)/.exec(pathname || '');
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Resolve the tenant for the current location and set window.APP_CONFIG.
 * Idempotent and side-effecting (writes window.APP_CONFIG).
 * @param {{hostname?: string, pathname?: string}} [loc] override for testing.
 * @returns {AppConfig}
 */
export function resolveTenant(loc) {
  const hostname = (loc && loc.hostname) ?? (typeof window !== 'undefined' ? window.location.hostname : '');
  const pathname = (loc && loc.pathname) ?? (typeof window !== 'undefined' ? window.location.pathname : '');

  const tenant = lookupTenant(hostname);
  const venueId = parseVenueFromPath(pathname);

  /** @type {AppConfig} */
  const appConfig = {
    apiBase: tenant.apiBase,
    firestoreDb: tenant.firestoreDb,
    firebaseProject: tenant.firebaseProject,
    brand: tenant.brand,
    venueId,
  };

  if (typeof window !== 'undefined') {
    window.APP_CONFIG = appConfig;
  }
  return appConfig;
}
