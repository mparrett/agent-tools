import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren, Timer } from 'claude-code'

import type { Attachment, FrontierView, WorkstreamView } from '../types'
import { colorSlots } from './model'
import { asOfLabel, attachments, badgeMode, badgeText, frontierView, roleLabel, touchesWorkstream } from './model'
import type { BadgeMode } from './model'

const PANE = 'workstream'
// A bare `/workstream` opens the pane or brings it forward; `/workstream close`
// closes it. The skill that attaches and
// resumes is `/workstream-manage`, a name of its own, so nothing here collides.
const COMMAND = 'workstream'
// `ls --session` is under a second, so the badge can keep close to the store;
// a frontier probes every anchor through gh and git (seconds per workstream),
// so the pane refreshes far less often, and only while it is open.
const BADGE_EVERY_MS = 60_000
const FRONTIER_EVERY_MS = 5 * 60_000

const INITIAL: WorkstreamView = { attached: null, frontiers: [], error: null, isLoading: false }
// Change the tag when WorkstreamView's fields change: a value outlives a reload.
const view = atom({ plugin: 'workstream', key: 'view' } as const, INITIAL, { shape: 'view-4' })
// The pane's foldable parts of each workstream, and whether each starts open:
// the sections do, save loose ends, a list that only grows and is read on
// purpose; the agent-facing instructions behind `how` do not.
const FOLDS = { ws: true, how: false, loose: false, anchors: true, sessions: true } as const
type Fold = keyof typeof FOLDS
// Theme colors for telling workstreams apart once some are folded; each keeps
// its own (colorSlots), and its next-step line is drawn in it too.
const WS_COLORS = ['claude', 'suggestion', 'merged', 'planMode', 'ide', 'remember'] as const
// `${workstream id}:${fold}` for each fold flipped from its default. The tag
// changes with FOLDS' defaults, since a flip stored against an old default
// would now mean the opposite.
const flipped = atom({ plugin: 'workstream', key: 'flipped' } as const, [] as string[], { shape: 'flipped-2' })
// The pane folded to its header and a line a workstream, by its [-] button.
const minimized = atom({ plugin: 'workstream', key: 'minimized' } as const, false)
// Minimized, a line each for this many workstreams, then a count of the rest;
// a count of one would take the row its line needs, so one more is drawn instead.
const MINIMIZED_ROWS = 2

// Where the badge goes, from the `badge` option. Options are fixed for an
// activation; a change reloads the plugin and runs `register` again.
let mode: BadgeMode = 'status'

// The CLI beside this mod, not one found on PATH: a session started without an
// interactive parent shell may not have ~/bin on PATH, and this copy always
// matches the hooks the plugin ships.
async function cli($: EngineInterface, args: string[]): Promise<string> {
  const bin = `${$.plugin.root}/../../bin/workstream`
  const r = await $.process.run([bin, ...args], { timeoutMs: 60_000 })
  if (r.exitCode !== 0) {
    const last = r.stderr.trim().split('\n').pop()
    throw new Error(last || `workstream ${args[0]} exited ${r.exitCode}`)
  }
  return r.stdout
}

async function isPaneOpen($: EngineInterface): Promise<boolean> {
  return (await $.ui.panes()).some(p => p.id === PANE)
}


async function refresh($: EngineInterface, withFrontier: boolean): Promise<void> {
  await update($, view, v => ({ ...v, isLoading: true }))
  let attached: Attachment[] | null = null
  let frontiers: FrontierView[] | undefined
  let error: string | null = null
  try {
    const sid = await $.session.id()
    attached = attachments(await cli($, ['ls', '--session', sid, '--json']))
    if (withFrontier) {
      // Built aside and kept only whole: one failed frontier keeps the last good list.
      const fresh: FrontierView[] = []
      for (const { name } of attached) {
        fresh.push(frontierView(await cli($, ['frontier', name, '--json'])))
      }
      frontiers = fresh
    }
  } catch (err) {
    error = err instanceof Error ? err.message : String(err)
  }
  const next = await update($, view, v => ({
    attached: attached ?? v.attached,
    frontiers: frontiers ?? v.frontiers,
    error,
    isLoading: false,
  }))
  await showBadge($, next)
}

// Where the pane is drawn now: set by its render, cleared when it closes. Not
// an atom: a render may not write state.
type Seat = 'dock' | 'inline' | 'widget' | null
let seat: Seat = null

// Inline (a narrow terminal seats the pane above the prompt) a pane listing
// the workstreams names each a line or two up, so the badge or the band would
// only repeat it on a row of its own. Only while it is drawn and listing them:
// one still reading the store, showing an error, or left waiting undrawn names
// nothing. No event says a pane stopped being drawn, so that is asked here each
// time; the badge timer's refresh brings the badge back within a minute.
async function paneNamesThem($: EngineInterface, v: WorkstreamView): Promise<boolean> {
  if (seat !== 'inline' || v.frontiers.length === 0 || v.error) return false
  const pane = (await $.ui.panes()).find(p => p.id === PANE)
  return !!(pane?.isPlaced && pane.isShown)
}

// The engine draws every plugin status line as a pinned warning (⚠, yellow);
// the band is the opt-in for a quieter badge. The band redraws on its own.
async function showBadge($: EngineInterface, v: WorkstreamView): Promise<void> {
  if (mode !== 'status') return $.ui.status(undefined)
  $.ui.status((await paneNamesThem($, v)) ? undefined : badgeText(v.attached, v.error))
}

async function setSeat($: EngineInterface, now: Seat): Promise<void> {
  if (now === seat) return
  seat = now
  await showBadge($, await read($, view))
  // The band redraws on a write to the view it reads, made once this render is done.
  // A failed redraw only leaves the band as it was until the next refresh rewrites the view.
  if (mode === 'band') $.clock.after(0, () => void update($, view, v => ({ ...v })).catch(() => undefined))
}

// One refresh at a time: a timer firing while a slow frontier runs would
// otherwise start a second one against the same store. A module variable is
// enough: a reload drops the timers that could overlap with it.
let running: Promise<void> | null = null
let runningHasFrontier = false
let timers: Timer[] = []

function kick($: EngineInterface, withFrontier: boolean): Promise<void> {
  if (running && (runningHasFrontier || !withFrontier)) return running
  // A frontier asked for while a badge-only refresh runs is queued after it,
  // not folded into it: the person pressed refresh or opened the pane.
  const mine: Promise<void> = (running ?? Promise.resolve())
    .catch(() => undefined)
    .then(() => refresh($, withFrontier))
    .finally(() => {
      if (running === mine) running = null
    })
  running = mine
  runningHasFrontier = withFrontier
  return mine
}

export const register: Register = (on, options) => {
  mode = badgeMode(options.badge)
  // A reload drops the panes' renders; the next one says where the pane is.
  seat = null

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: COMMAND, description: 'Open the workstream pane or bring it forward; /workstream close closes it' })
    // Whatever started before is stopped first, so a second start never doubles the polling.
    for (const t of timers) t.cancel()
    timers = [
      $.clock.every(BADGE_EVERY_MS, () => void kick($, false)),
      $.clock.every(FRONTIER_EVERY_MS, async () => {
        if (await isPaneOpen($)) void kick($, true)
      }),
    ]
    // Not awaited: the first prompt need not wait on the store.
    void kick($, false).then(async () => {
      const { attached } = await read($, view)
      // Opened unasked only for an attached session; the surface seats an
      // unasked pane only where it fits beside the transcript.
      if (attached?.length) {
        await $.ui.open({ id: PANE, title: 'Workstream' })
        void kick($, true)
      }
    })
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const pane = (await $.ui.panes()).find(p => p.id === PANE)
    if (arg === 'close' || arg === 'hide') {
      if (!pane) return { text: 'The workstream pane is not open.' }
      await $.ui.close({ id: PANE })
      return { text: 'Workstream pane closed. /workstream reopens it.' }
    }
    if (arg) return { text: 'Usage: /workstream [close]; /workstream-manage runs the skill.' }
    // One waiting undrawn on a narrow terminal is closed, so a second
    // /workstream cancels it instead of leaving its frontier polling.
    if (pane && !pane.isPlaced) {
      await $.ui.close({ id: PANE })
      return { text: 'Workstream pane closed. /workstream reopens it.' }
    }
    // Otherwise open, or raise and focus: a re-open of an open pane only
    // retitles it, so one behind another pane's tab needs `focus` to come
    // forward. Typing the command means the prompt held the keys, so the pane
    // was never the focused one to toggle off.
    const opened = await $.ui.open({ id: PANE, title: 'Workstream', focus: true })
    // Only a fresh open fetches the frontier: raising an open pane is not a
    // refresh, and the timer keeps it current.
    if (!pane) void kick($, true)
    if (!opened.isPlaced) return { text: `Workstream pane waiting: ${opened.reason}. /workstream cancels it.` }
    return { text: pane ? 'Workstream pane brought forward. /workstream close closes it.' : 'Workstream pane opened. /workstream close closes it.' }
  }).catch(() => ({ text: 'The workstream pane could not be opened or closed; see claude --debug.' }))

  // Never a gate: a failed refresh must not refuse the command it watched.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (touchesWorkstream(e.command)) void kick($, await isPaneOpen($))
    return ran
  }).catch(($, e, next) => next(e))

  // Two colors: dim chrome and roles, the names in the theme's suggestion color.
  // Store errors are the one thing drawn as a warning.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (mode !== 'band' || e.props.hasSurvey) return next(e)
    const v = await read($, view)
    if (v.attached === null && !v.error) return next(e)
    // Attached to none, the band has nothing to say; a pane drawn inline and
    // listing the workstreams says it already.
    if ((v.attached?.length === 0 && !v.error) || (await paneNamesThem($, v))) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row">
        <Text color="inactive">◆ ws </Text>
        {v.attached === null ? (
          <Text color="warning">unknown (store unreadable)</Text>
        ) : v.attached.length === 0 ? (
          <Text color="inactive">none</Text>
        ) : (
          v.attached.map((a, i) => (
            <Text>
              {i > 0 ? <Text color="inactive">, </Text> : null}
              <Text color="suggestion">{a.name}</Text>
              <Text color="inactive"> · {roleLabel(a)}</Text>
            </Text>
          ))
        )}
        {v.attached !== null && v.error ? <Text color="warning"> (stale: store unreadable)</Text> : null}
      </Box>
    )
  })

  // A pane closed by hand, by `/workstream close`, or with its plugin: the badge comes back.
  on('ui.close', async ($, e, next) => {
    const done = await next(e)
    if (e.id === PANE) await setSeat($, null)
    return done
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    // Any seat the engine adds later counts as `dock`, as its docs ask.
    const p = e.props.placement
    void setSeat($, p === 'inline' || p === 'widget' ? p : 'dock')
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const v = await read($, view)
    const flips = await read($, flipped)
    const isMin = await read($, minimized)
    const isOpen = (id: string, fold: Fold) => FOLDS[fold] !== flips.includes(`${id}:${fold}`)
    const toggle = async (id: string, fold: Fold) => {
      const k = `${id}:${fold}`
      // Workstreams no longer shown are dropped here, so the list cannot outgrow
      // the pane; against the view as it is now, not as this render saw it.
      const now = await read($, view)
      return update($, flipped, xs => {
        const kept = xs.filter(x => x !== k && now.frontiers.some(f => x.startsWith(`${f.id}:`)))
        return xs.includes(k) ? kept : [...kept, k]
      })
    }
    // A section's header is its own toggle, caret first; folded, it gives a count.
    const section = (id: string, color: string, fold: Fold, title: string, count: number, rows: RenderChildren[]) => {
      const shut = !isOpen(id, fold)
      return (
        <Box flexDirection="column" marginTop={1}>
          <Button plain key={`${fold}:${id}`} label={title} onPress={() => toggle(id, fold)}>
            <Text color={color}>{shut ? '▸ ' : '▾ '}</Text>
            <Text weight="semibold">{title}</Text>
            {shut ? <Text dimColor> ({count})</Text> : null}
          </Button>
          {shut ? null : rows}
        </Box>
      )
    }
    // [-] and refresh lead the row, as the HUD's [-] does: at the right they sat
    // under the pane's own close mark, where a press meant for one landed on the other.
    const header = (
      <Box flexDirection="row">
        <Box flexShrink={0} marginRight={1}>
          <Button plain key="size" label={isMin ? 'Restore' : 'Minimize'} onPress={() => update($, minimized, m => !m)}>
            <Text dimColor>{isMin ? '[+]' : '[-]'}</Text>
          </Button>
        </Box>
        <Box flexShrink={0} marginRight={1}>
          <Button key="refresh" label="refresh" onPress={() => kick($, true)} />
        </Box>
        <Text dimColor>{v.isLoading ? 'refreshing…' : asOfLabel(v.frontiers[0]?.asOf ?? null)}</Text>
      </Box>
    )
    if (v.error) {
      return (
        <Box flexDirection="column">
          {header}
          <Text color="error">{v.error}</Text>
        </Box>
      )
    }
    if (v.attached === null || (v.attached.length > 0 && v.frontiers.length === 0)) {
      return (
        <Box flexDirection="column">
          {header}
          <Text dimColor>Reading the workstream store…</Text>
        </Box>
      )
    }
    if (v.attached.length === 0) {
      return (
        <Box flexDirection="column">
          {header}
          <Text>This session is attached to no workstream.</Text>
          <Text dimColor>workstream attach NAME, or --new "TITLE" --name SLUG</Text>
        </Box>
      )
    }
    const slots = colorSlots(
      v.frontiers.map(f => f.id),
      WS_COLORS.length,
    )
    if (isMin) {
      const shown = v.frontiers.length <= MINIMIZED_ROWS + 1 ? v.frontiers.length : MINIMIZED_ROWS
      const rest = v.frontiers.length - shown
      return (
        <Box flexDirection="column">
          {header}
          {v.frontiers.slice(0, shown).map((f, i) => {
            const color = WS_COLORS[slots.get(f.id) ?? i % WS_COLORS.length] ?? 'claude'
            return (
              <Text key={f.id} wrap="truncate-end">
                <Text color={color}>◆ </Text>
                <Text color={color} weight="semibold">
                  {f.name}
                </Text>
                <Text dimColor> [{f.state}]</Text>
                {f.next ? (
                  <Text color={color}>
                    {' '}
                    next: {f.next.action}
                    {f.next.session ? ` ${f.next.session}` : ''}
                  </Text>
                ) : null}
              </Text>
            )
          })}
          {rest > 0 ? <Text dimColor>+{rest} more</Text> : null}
        </Box>
      )
    }
    return (
      <Box flexDirection="column">
        {header}
        {v.frontiers.map((f, i) => {
          const color = WS_COLORS[slots.get(f.id) ?? i % WS_COLORS.length] ?? 'claude'
          const shut = !isOpen(f.id, 'ws')
          // The workstream's own header: a rule in its color to the pane's edge,
          // which also folds the whole workstream down to that one line.
          const lead = `${shut ? '▸' : '▾'} ◆ ${f.name} [${f.state}] `
          // The pane's own width; `e.viewport` is the whole terminal's.
          // Code points, not UTF-16 units; none at all when the name fills the line.
          const rule = '─'.repeat(Math.max(0, (e.props.bodyColumns ?? 38) - [...lead].length))
          return (
            <Box flexDirection="column" marginTop={1}>
              <Button plain key={`ws:${f.id}`} label={f.name} onPress={() => toggle(f.id, 'ws')}>
                <Text color={color}>{shut ? '▸ ◆ ' : '▾ ◆ '}</Text>
                <Text color={color} weight="semibold">
                  {f.name}
                </Text>
                <Text dimColor> [{f.state}] </Text>
                <Text color={color}>{rule}</Text>
              </Button>
              {shut ? null : (
                <Box flexDirection="column">
                  <Text>{f.title}</Text>
                  {f.next && (
                    <Box flexDirection="row" justifyContent="space-between">
                      <Text>
                        <Text color={color}>
                          next: {f.next.action}
                          {f.next.session ? ` ${f.next.session}` : ''}
                        </Text>
                        {f.next.why ? <Text dimColor> · {f.next.why}</Text> : null}
                      </Text>
                      {f.next.how || f.next.facts.length ? (
                        <Button plain dimColor key={`how:${f.id}`} label="how" onPress={() => toggle(f.id, 'how')}>
                          {isOpen(f.id, 'how') ? '▾ how' : '▸ how'}
                        </Button>
                      ) : null}
                    </Box>
                  )}
                  {f.next && isOpen(f.id, 'how') && (
                    <Box flexDirection="column">
                      {f.next.how ? <Text dimColor>  {f.next.how}</Text> : null}
                      {f.next.facts.map(x => (
                        <Text dimColor>  • {x}</Text>
                      ))}
                    </Box>
                  )}
                  {f.looseEnds.length > 0 &&
                    section(
                      f.id,
                      color,
                      'loose',
                      'loose ends',
                      f.looseEnds.length,
                      f.looseEnds.map(l => <Text color="warning">  • {l}</Text>),
                    )}
                  {f.anchors.length > 0 &&
                    section(
                      f.id,
                      color,
                      'anchors',
                      'anchors',
                      f.anchors.length,
                      f.anchors.map(a => (
                        <Text>
                          {'  '}
                          <Text dimColor>{a.status.padEnd(9)}</Text>
                          {a.href ? <Link href={a.href} label={a.ref} /> : a.ref}
                        </Text>
                      )),
                    )}
                  {f.sessions.length > 0 &&
                    section(
                      f.id,
                      color,
                      'sessions',
                      'sessions',
                      f.sessions.length,
                      f.sessions.map(s => (
                        <Text>
                          {'  '}
                          {s.id} <Text dimColor>{s.role}</Text>
                          {s.live ? <Text color="success"> live</Text> : null}
                        </Text>
                      )),
                    )}
                  {f.prose && (
                    <Box marginTop={1}>
                      <Text dimColor>
                        prose: {f.prose.path}
                        {f.prose.exists ? '' : ' (not written yet)'}
                      </Text>
                    </Box>
                  )}
                </Box>
              )}
            </Box>
          )
        })}
      </Box>
    )
  })
}
