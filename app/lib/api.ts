/** API helpers for browser → backend (API key + optional warmup). */
export const API_BASE =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) ||
  'http://localhost:8000'

export const WARMUP_URL =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_WARMUP_URL) || ''

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

export async function warmupBackend(): Promise<void> {
  try {
    if (WARMUP_URL) {
      await fetch(WARMUP_URL, { method: 'GET', mode: 'cors' })
      return
    }
    await fetch(`${API_BASE}/warmup`, {
      method: 'GET',
      headers: apiHeaders(),
    })
  } catch {
    /* best-effort */
  }
}

export async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers || {})
  const key = getStoredApiKey()
  if (key) headers.set('X-API-Key', key)
  return fetch(`${API_BASE}${path}`, { ...init, headers })
}
