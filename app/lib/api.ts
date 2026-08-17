/** API helpers for browser → CloudFront /api (Lambda control plane). */
function defaultApiBase(): string {
  if (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) {
    return process.env.NEXT_PUBLIC_API_URL.replace(/\/$/, '')
  }
  // Same-origin /api via CloudFront when UI is served over HTTPS
  if (typeof window !== 'undefined' && window.location.protocol === 'https:') {
    return `${window.location.origin}/api`
  }
  return 'http://localhost:8000'
}

export const API_BASE = defaultApiBase()

const KEY_STORAGE = 'autocaption_api_key'

export function getStoredApiKey(): string {
  if (typeof window === 'undefined') return ''
  return sessionStorage.getItem(KEY_STORAGE) || ''
}

export function setStoredApiKey(key: string) {
  if (typeof window === 'undefined') return
  if (key) sessionStorage.setItem(KEY_STORAGE, key)
  else sessionStorage.removeItem(KEY_STORAGE)
}

export function apiHeaders(extra?: HeadersInit): HeadersInit {
  const key = getStoredApiKey()
  return {
    ...(extra || {}),
    ...(key ? { 'X-API-Key': key } : {}),
  }
}

export async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {})
  const key = getStoredApiKey()
  if (key) headers.set('X-API-Key', key)
  return fetch(`${API_BASE}${path}`, { ...init, headers })
}
