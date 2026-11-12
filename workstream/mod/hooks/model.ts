import type { Attachment, FrontierView } from '../types'

type Json = Record<string, unknown>

const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const obj = (v: unknown): Json => (v && typeof v === 'object' ? (v as Json) : {})
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

// `workstream ls --session SID --json` → what the badge shows: each workstream
// by name (an unnamed one by its short id, as the CLI prints it), and whether
// this session owns it. The CLI decides ownership (`owned`: a live canonical
// claim; an `--ancillary` attach takes none) so the badge and the CLI agree.
export function attachments(lsJson: string): Attachment[] {
  return arr(JSON.parse(lsJson)).map(w => {
    const o = obj(w)
    return { name: str(o.name) || str(o.id).slice(0, 12), owner: o.owned === true }
  })
}

export const roleLabel = (a: Attachment): string => (a.owner ? 'owner' : 'helping')

// The engine already heads the status line with the plugin's name, "workstream:"
export function badgeText(attached: Attachment[] | null, error: string | null): string | undefined {
  if (attached === null) return error ? 'unknown (store unreadable)' : undefined
  const text = attached.length ? attached.map(a => `${a.name} (${roleLabel(a)})`).join(', ') : 'none'
  // A failed refresh keeps the last list; say so rather than pass it off as current.
  return error ? `${text} (stale: store unreadable)` : text
}

export type BadgeMode = 'status' | 'band' | 'off'

// The `badge` option; a missing or unknown value keeps the status line, which
// is what the plugin did before the option existed.
export function badgeMode(value: unknown): BadgeMode {
  return value === 'band' || value === 'off' ? value : 'status'
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
  const options = obj(d.options)
  // `options.default` names the real choice; `recommendation.action` folds
  // `message` into `fresh` for older readers, so it is only the fallback.
  const action = str(options.default) || str(obj(d.recommendation).action)
  const option = obj(options[action])
  const fresh = obj(obj(options.fresh).data)
  // Only resume and message act on a particular session.
  const target = action === 'resume' || action === 'message' ? str(options.session_id).slice(0, 8) : ''
  return {
    id: str(ws.id),
    name: str(ws.name) || str(ws.id).slice(0, 12),
    title: str(ws.title),
    state: str(ws.state),
    next: action
      ? {
          action,
          session: target,
          // `why` is written for the person; `how` and `facts` are the agent's
          // instructions, which the pane keeps folded until asked.
          why: str(options.default_why) || arr(obj(d.recommendation).why).map(str).filter(Boolean)[0] || '',
          how: str(option.how),
          facts: arr(option.facts).map(str).filter(Boolean),
        }
      : null,
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

// A color index for each workstream id, out of `n`: from a hash of the id, so a
// workstream keeps its color as others attach or detach, then the next free
// slot on a collision, so the ones shown together never share one (until
// there are more than `n`). Ids claim slots in sorted order, not drawn order,
// so only a collision with an id sorting earlier moves a workstream's color.
export function colorSlots(ids: string[], n: number): Map<string, number> {
  const slots = new Map<string, number>()
  const taken = new Set<number>()
  for (const id of [...new Set(ids)].sort()) {
    let h = 0
    for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0
    let slot = h % n
    for (let k = 0; k < n && taken.has(slot); k++) slot = (slot + 1) % n
    taken.add(slot)
    slots.set(id, slot)
  }
  return slots
}
