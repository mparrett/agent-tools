import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren, Timer } from 'claude-code'

import type { Attachment, FrontierView, WorkstreamView } from '../types'
import { asOfLabel, attachments, badgeMode, badgeText, frontierView, roleLabel, touchesWorkstream } from './model'
import type { BadgeMode } from './model'

const PANE = 'workstream'
const COMMAND = 'workstream-pane'
// `ls --session` is under a second, so the badge can keep close to the store;
// a frontier probes every anchor through gh and git (seconds per workstream),
// so the pane refreshes far less often, and only while it is open.
const BADGE_EVERY_MS = 60_000
const FRONTIER_EVERY_MS = 5 * 60_000

const INITIAL: WorkstreamView = { attached: null, frontiers: [], error: null, isLoading: false }
// Change the tag when WorkstreamView's fields change: a value outlives a reload.
const view = atom({ plugin: 'workstream', key: 'view' } as const, INITIAL, { shape: 'view-3' })
// The pane's foldable parts of each workstream, and whether each starts open:
// the sections do; the agent-facing instructions behind `how` do not.
const FOLDS = { how: false, loose: true, anchors: true, sessions: true } as const
type Fold = keyof typeof FOLDS
// `${workstream id}:${fold}` for each fold flipped from its default.
const flipped = atom({ plugin: 'workstream', key: 'flipped' } as const, [] as string[], { shape: 'flipped-1' })

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
  // The engine draws every plugin status line as a pinned warning (⚠, yellow);
  // the band is the opt-in for a quieter badge. The band redraws on the write above.
  $.ui.status(mode === 'status' ? badgeText(next.attached, next.error) : undefined)
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

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: COMMAND,
      description: "Open the workstream pane: this session's workstreams and their frontier",
    })
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

  on('command.run', { command: COMMAND }, async $ => {
    await $.ui.open({ id: PANE, title: 'Workstream' })
    void kick($, true)
    return { text: 'Workstream pane opened.' }
  }).catch(() => ({ text: 'The workstream pane could not open; see claude --debug.' }))

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

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const v = await read($, view)
    const flips = await read($, flipped)
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
    const section = (id: string, fold: Fold, title: string, count: number, rows: RenderChildren[]) => {
      const shut = !isOpen(id, fold)
      return (
        <Box flexDirection="column" marginTop={1}>
          <Button plain key={`${fold}:${id}`} label={title} onPress={() => toggle(id, fold)}>
            <Text dimColor>{shut ? '▸ ' : '▾ '}</Text>
            <Text weight="semibold">{title}</Text>
            {shut ? <Text dimColor> ({count})</Text> : null}
          </Button>
          {shut ? null : rows}
        </Box>
      )
    }
    const header = (
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor>{v.isLoading ? 'refreshing…' : asOfLabel(v.frontiers[0]?.asOf ?? null)}</Text>
        <Button key="refresh" label="refresh" onPress={() => kick($, true)} />
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
    return (
      <Box flexDirection="column">
        {header}
        {v.frontiers.map(f => (
          <Box flexDirection="column" marginTop={1}>
            <Text>
              <Text weight="semibold">{f.name}</Text> <Text dimColor>[{f.state}]</Text>
            </Text>
            <Text>{f.title}</Text>
            {f.next && (
              <Box flexDirection="row" justifyContent="space-between">
                <Text>
                  <Text color="suggestion">
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
                'loose',
                'loose ends',
                f.looseEnds.length,
                f.looseEnds.map(l => <Text color="warning">  • {l}</Text>),
              )}
            {f.anchors.length > 0 &&
              section(
                f.id,
                'anchors',
                'anchors',
                f.anchors.length,
                f.anchors.map(a => (
                  <Text>
                    {'  '}
                    <Text dimColor>{a.status.padEnd(9)}</Text>
                    {a.ref}
                  </Text>
                )),
              )}
            {f.sessions.length > 0 &&
              section(
                f.id,
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
        ))}
      </Box>
    )
  })
}
