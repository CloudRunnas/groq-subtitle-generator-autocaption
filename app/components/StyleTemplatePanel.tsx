'use client'

import React, { useCallback, useEffect, useState } from 'react'

import { API_BASE, apiFetch } from '../lib/api'

const API = API_BASE

export type TextRoleStyle = {
  color: string
  formatting: 'normal' | 'bold' | 'italic' | 'bold_italic'
  strokeColor?: string | null
  strokeWidth?: number | null
}

export type StyleTemplate = {
  version: number
  name: string
  slug?: string
  fontS3Key?: string | null
  fontName?: string | null
  strokeColor: string
  strokeWidth: number
  spokenText: TextRoleStyle
  activeText: TextRoleStyle
  normalText: TextRoleStyle
  effects: {
    bounce: { enabled: boolean; scalePercent: number; upMs: number; downMs: number }
    outlinePulse: { enabled: boolean; peakWidth: number; upMs: number; downMs: number }
  }
}

const defaultRole = (color: string, formatting: TextRoleStyle['formatting'] = 'normal'): TextRoleStyle => ({
  color,
  formatting,
  strokeColor: '#000000',
  strokeWidth: 4,
})

export const emptyTemplate = (): StyleTemplate => ({
  version: 1,
  name: '',
  fontS3Key: null,
  strokeColor: '#000000',
  strokeWidth: 4,
  spokenText: defaultRole('#FFFFFF', 'bold'),
  activeText: defaultRole('#F97316', 'bold'),
  normalText: defaultRole('#FFFFFF', 'normal'),
  effects: {
    bounce: { enabled: true, scalePercent: 135, upMs: 80, downMs: 100 },
    outlinePulse: { enabled: true, peakWidth: 10, upMs: 100, downMs: 120 },
  },
})

type Props = {
  selectedSlug: string
  onSelectSlug: (slug: string) => void
}

function ColorField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="block text-sm">
      <span className="text-gray-700 font-medium">{label}</span>
      <div className="mt-1 flex items-center gap-2">
        <input
          type="color"
          value={value?.startsWith('#') ? value : `#${value}`}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          className="h-9 w-12 cursor-pointer rounded border border-gray-300 bg-white p-0.5"
        />
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 rounded-lg border border-gray-300 px-3 py-2 font-mono text-sm"
          placeholder="#FFFFFF"
        />
        <span
          className="h-9 w-9 rounded border border-gray-300"
          style={{ backgroundColor: value || '#000' }}
          title={value}
        />
      </div>
    </label>
  )
}

function RoleEditor({
  title,
  role,
  onChange,
}: {
  title: string
  role: TextRoleStyle
  onChange: (r: TextRoleStyle) => void
}) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
      <h4 className="font-semibold text-gray-900">{title}</h4>
      <ColorField label="Color" value={role.color} onChange={(color) => onChange({ ...role, color })} />
      <label className="block text-sm">
        <span className="text-gray-700 font-medium">Formatting</span>
        <select
          value={role.formatting}
          onChange={(e) => onChange({ ...role, formatting: e.target.value as TextRoleStyle['formatting'] })}
          className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2"
        >
          <option value="normal">normal</option>
          <option value="bold">bold</option>
          <option value="italic">italic</option>
          <option value="bold_italic">bold_italic</option>
        </select>
      </label>
      <ColorField
        label="Stroke color (override)"
        value={role.strokeColor || '#000000'}
        onChange={(strokeColor) => onChange({ ...role, strokeColor })}
      />
      <label className="block text-sm">
        <span className="text-gray-700 font-medium">Stroke width (override)</span>
        <input
          type="number"
          min={0}
          max={40}
          step={0.5}
          value={role.strokeWidth ?? 4}
          onChange={(e) => onChange({ ...role, strokeWidth: Number(e.target.value) })}
          className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2"
        />
      </label>
    </div>
  )
}

export default function StyleTemplatePanel({ selectedSlug, onSelectSlug }: Props) {
  const [templates, setTemplates] = useState<Array<{ name: string; slug: string }>>([])
  const [fonts, setFonts] = useState<Array<{ name: string; s3Key: string }>>([])
  const [draft, setDraft] = useState<StyleTemplate>(emptyTemplate())
  const [previews, setPreviews] = useState<Array<{ index: number; strokeWidth: number; url: string | null }>>([])
  const [saveName, setSaveName] = useState('')
  const [showSaveDialog, setShowSaveDialog] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  const loadLists = useCallback(async () => {
    try {
      const [tRes, fRes, pRes] = await Promise.all([
        apiFetch('/styles/templates'),
        apiFetch('/styles/fonts'),
        apiFetch('/styles/stroke-previews'),
      ])
      if (tRes.ok) {
        const data = await tRes.json()
        setTemplates(data.templates || [])
      }
      if (fRes.ok) {
        const data = await fRes.json()
        setFonts(data.fonts || [])
      }
      if (pRes.ok) {
        const data = await pRes.json()
        setPreviews(data.previews || [])
      }
    } catch (e) {
      console.error(e)
    }
  }, [])

  const loadTemplate = useCallback(async (slug: string) => {
    if (!slug) {
      setDraft(emptyTemplate())
      return
    }
    const res = await apiFetch(`/styles/templates/${slug}`)
    if (!res.ok) return
    const data = await res.json()
    setDraft({ ...emptyTemplate(), ...data })
    onSelectSlug(slug)
  }, [onSelectSlug])

  useEffect(() => {
    loadLists()
  }, [loadLists])

  useEffect(() => {
    if (selectedSlug) loadTemplate(selectedSlug)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const saveTemplate = async () => {
    const name = saveName.trim() || draft.name.trim()
    if (!name) {
      setMessage('Bitte einen Namen angeben.')
      return
    }
    setBusy(true)
    setMessage('')
    try {
      const payload = { ...draft, name, strokeWidth: Number(draft.strokeWidth) }
      const res = await apiFetch('/styles/templates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res.ok) throw new Error(await res.text())
      const saved = await res.json()
      setShowSaveDialog(false)
      setSaveName('')
      await loadLists()
      if (saved.slug) {
        onSelectSlug(saved.slug)
        setDraft({ ...emptyTemplate(), ...saved })
      }
      setMessage(`Template „${saved.name}“ gespeichert.`)
    } catch (e: any) {
      setMessage(e?.message || 'Speichern fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const uploadFont = async (file: File | null) => {
    if (!file) return
    setBusy(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const res = await apiFetch('/styles/fonts', { method: 'POST', body: fd })
      if (!res.ok) throw new Error(await res.text())
      const asset = await res.json()
      await loadLists()
      setDraft((d) => ({ ...d, fontS3Key: asset.s3Key, fontName: asset.name }))
      setMessage(`Font „${asset.name}“ hochgeladen.`)
    } catch (e: any) {
      setMessage(e?.message || 'Font-Upload fehlgeschlagen')
    } finally {
      setBusy(false)
    }
  }

  const nearestPreviewIndex = (() => {
    if (!previews.length) return 0
    let best = 0
    let bestDiff = Infinity
    previews.forEach((p, i) => {
      const d = Math.abs(p.strokeWidth - draft.strokeWidth)
      if (d < bestDiff) {
        bestDiff = d
        best = i
      }
    })
    return best
  })()

  return (
    <div className="bg-gray-50 rounded-lg p-5 border border-gray-200 space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-lg font-semibold text-gray-900">Karaoke Style Template</h3>
        <select
          value={selectedSlug}
          onChange={(e) => {
            const slug = e.target.value
            onSelectSlug(slug)
            loadTemplate(slug)
          }}
          className="rounded-lg border border-gray-300 px-3 py-2 bg-white min-w-[200px]"
        >
          <option value="">— Standard —</option>
          {templates.map((t) => (
            <option key={t.slug} value={t.slug}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        <RoleEditor
          title="spokenText (bereits gesprochen)"
          role={draft.spokenText}
          onChange={(spokenText) => setDraft({ ...draft, spokenText })}
        />
        <RoleEditor
          title="activeText (aktuelles Wort)"
          role={draft.activeText}
          onChange={(activeText) => setDraft({ ...draft, activeText })}
        />
        <RoleEditor
          title="normalText (noch nicht)"
          role={draft.normalText}
          onChange={(normalText) => setDraft({ ...draft, normalText })}
        />
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
          <h4 className="font-semibold text-gray-900">Global Stroke & Font</h4>
          <ColorField
            label="strokeColor"
            value={draft.strokeColor}
            onChange={(strokeColor) => setDraft({ ...draft, strokeColor })}
          />
          <label className="block text-sm">
            <span className="text-gray-700 font-medium">strokeWidth: {draft.strokeWidth}</span>
            <input
              type="range"
              min={0.5}
              max={8}
              step={0.5}
              value={draft.strokeWidth}
              onChange={(e) => setDraft({ ...draft, strokeWidth: Number(e.target.value) })}
              className="mt-2 w-full"
            />
          </label>
          {previews.length > 0 && (
            <div className="grid grid-cols-4 gap-2">
              {previews.map((p, i) => (
                <button
                  key={p.index}
                  type="button"
                  onClick={() => setDraft({ ...draft, strokeWidth: p.strokeWidth })}
                  className={`rounded border overflow-hidden ${
                    i === nearestPreviewIndex ? 'border-orange-500 ring-2 ring-orange-300' : 'border-gray-200'
                  }`}
                  title={`strokeWidth ${p.strokeWidth}`}
                >
                  {p.url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={`${API}${p.url}`}
                      alt={`stroke ${p.strokeWidth}`}
                      className="w-full h-12 object-cover"
                    />
                  ) : (
                    <div className="h-12 bg-gray-100 text-[10px] flex items-center justify-center">
                      {p.strokeWidth}
                    </div>
                  )}
                </button>
              ))}
            </div>
          )}
          <label className="block text-sm">
            <span className="text-gray-700 font-medium">Font</span>
            <select
              value={draft.fontS3Key || ''}
              onChange={(e) => {
                const key = e.target.value || null
                const font = fonts.find((f) => f.s3Key === key)
                setDraft({ ...draft, fontS3Key: key, fontName: font?.name || null })
              }}
              className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2"
            >
              <option value="">System / Arial</option>
              {fonts.map((f) => (
                <option key={f.s3Key} value={f.s3Key}>
                  {f.name}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-flex items-center gap-2 text-sm text-orange-600 cursor-pointer">
            <span>Font hochladen (.ttf / .otf)</span>
            <input
              type="file"
              accept=".ttf,.otf,font/ttf,font/otf"
              className="hidden"
              onChange={(e) => uploadFont(e.target.files?.[0] || null)}
            />
          </label>
        </div>

        <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-4">
          <h4 className="font-semibold text-gray-900">Effekte (nur activeText)</h4>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.effects.bounce.enabled}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  effects: {
                    ...draft.effects,
                    bounce: { ...draft.effects.bounce, enabled: e.target.checked },
                  },
                })
              }
            />
            Bounce
          </label>
          <label className="block text-sm">
            scalePercent: {draft.effects.bounce.scalePercent}
            <input
              type="range"
              min={100}
              max={200}
              value={draft.effects.bounce.scalePercent}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  effects: {
                    ...draft.effects,
                    bounce: { ...draft.effects.bounce, scalePercent: Number(e.target.value) },
                  },
                })
              }
              className="w-full"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-sm">
              upMs
              <input
                type="number"
                value={draft.effects.bounce.upMs}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    effects: {
                      ...draft.effects,
                      bounce: { ...draft.effects.bounce, upMs: Number(e.target.value) },
                    },
                  })
                }
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
              />
            </label>
            <label className="text-sm">
              downMs
              <input
                type="number"
                value={draft.effects.bounce.downMs}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    effects: {
                      ...draft.effects,
                      bounce: { ...draft.effects.bounce, downMs: Number(e.target.value) },
                    },
                  })
                }
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
              />
            </label>
          </div>

          <label className="flex items-center gap-2 text-sm pt-2">
            <input
              type="checkbox"
              checked={draft.effects.outlinePulse.enabled}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  effects: {
                    ...draft.effects,
                    outlinePulse: { ...draft.effects.outlinePulse, enabled: e.target.checked },
                  },
                })
              }
            />
            Outline-Pulse
          </label>
          <label className="block text-sm">
            peakWidth: {draft.effects.outlinePulse.peakWidth}
            <input
              type="range"
              min={0}
              max={24}
              step={0.5}
              value={draft.effects.outlinePulse.peakWidth}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  effects: {
                    ...draft.effects,
                    outlinePulse: {
                      ...draft.effects.outlinePulse,
                      peakWidth: Number(e.target.value),
                    },
                  },
                })
              }
              className="w-full"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-sm">
              upMs
              <input
                type="number"
                value={draft.effects.outlinePulse.upMs}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    effects: {
                      ...draft.effects,
                      outlinePulse: {
                        ...draft.effects.outlinePulse,
                        upMs: Number(e.target.value),
                      },
                    },
                  })
                }
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
              />
            </label>
            <label className="text-sm">
              downMs
              <input
                type="number"
                value={draft.effects.outlinePulse.downMs}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    effects: {
                      ...draft.effects,
                      outlinePulse: {
                        ...draft.effects.outlinePulse,
                        downMs: Number(e.target.value),
                      },
                    },
                  })
                }
                className="mt-1 w-full rounded border border-gray-300 px-2 py-1"
              />
            </label>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setSaveName(draft.name || '')
            setShowSaveDialog(true)
          }}
          className="px-5 py-2.5 bg-orange-500 text-white rounded-lg font-medium hover:bg-orange-600 disabled:opacity-50"
        >
          Template speichern
        </button>
        {message && <p className="text-sm text-gray-600">{message}</p>}
      </div>

      {showSaveDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-white rounded-xl shadow-xl max-w-md w-full p-6 space-y-4">
            <h4 className="text-lg font-semibold text-gray-900">Template speichern</h4>
            <label className="block text-sm">
              Name
              <input
                autoFocus
                value={saveName}
                onChange={(e) => setSaveName(e.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-300 px-3 py-2"
                placeholder="z.B. Neon Gelb"
              />
            </label>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowSaveDialog(false)}
                className="px-4 py-2 rounded-lg border border-gray-300"
              >
                Abbrechen
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={saveTemplate}
                className="px-4 py-2 rounded-lg bg-orange-500 text-white"
              >
                Speichern
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
