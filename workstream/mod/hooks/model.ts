import type { FrontierView } from '../types'

type Json = Record<string, unknown>

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const obj = (v: unknown): Json => (v && typeof v === 'object' ? (v as Json) : {})
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

// `workstream ls --json` → the names a badge shows; an unnamed workstream by its
// short id, as the CLI prints it
export function attachedNames(lsJson: string): string[] {
  return arr(JSON.parse(lsJson)).map(w => {
    const o = obj(w)
    return str(o.name) || str(o.id).slice(0, 12)
  })
}

// The engine already heads the status line with the plugin's name, "workstream:"
export function badgeText(attached: string[] | null, error: string | null): string | undefined {
  if (attached === null) return error ? 'unknown (store unreadable)' : undefined
  return attached.length ? attached.join(', ') : 'none'
}

// "2026-11-11T00:54:06Z (16:54 -08:00)", the CLI's Instant.iso(), → "as of 16:54"
export function asOfLabel(asOf: string | null): string {
  const local = asOf && /\((\d\d:\d\d)/.exec(asOf)
  return local ? `as of ${local[1]}` : ''
}

// `workstream frontier --json` → what the pane draws
export function frontierView(json: string): FrontierView {
  const d = obj(JSON.parse(json))
  const ws = obj(d.workstream)
  const rec = obj(d.recommendation)
  const action = str(rec.action)
  const option = obj(obj(d.options)[action])
  const fresh = obj(obj(obj(d.options).fresh).data)
  return {
    name: str(ws.name) || str(ws.id).slice(0, 12),
    title: str(ws.title),
    state: str(ws.state),
    next: action ? { action, how: str(option.how) } : null,
    looseEnds: arr(d.loose_ends).map(str),
    anchors: arr(d.anchors).map(a => ({ ref: str(obj(a).ref), status: str(obj(a).status) })),
    sessions: arr(d.sessions).map(s => {
      const o = obj(s)
      return { id: str(o.id).slice(0, 8), role: str(o.role), live: o.live === true }
    }),
    prose: str(fresh.prose_frontier)
      ? { path: str(fresh.prose_frontier), exists: fresh.prose_exists === true }
      : null,
    asOf: str(d.as_of) || null,
  }
}

// Bash commands after which what the badge or pane shows may have changed
export function touchesWorkstream(command: string): boolean {
  // In command position only (start, or after ; & |), so `rg workstream attach` is not one
  return /(^|[;&|])\s*(\S*\/)?workstream\s+(attach|close|retire|sync|frontier\s.*--record)/.test(command)
}
