import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { asOfLabel, attachments, badgeMode, badgeText, frontierView, touchesWorkstream } from './model'

// Trimmed from a real `workstream frontier --json`: only the fields the pane reads.
const FRONTIER = JSON.stringify({
  workstream: { id: 'abc123def4567890', name: 'demo', title: 'A demo', state: 'open' },
  as_of: '2026-11-11T00:48:09Z (16:48 -08:00)',
  loose_ends: ['5d5c9a2e (canonical) has no recorded frontier'],
  anchors: [{ ref: 'gh:o/r#7', status: 'open' }],
  sessions: [{ id: '5d5c9a2e-6027-4916-bd97-eebadc8905d2', role: 'canonical', live: true }],
  recommendation: { action: 'message' },
  options: {
    message: { how: 'SendMessage to "demo-session"' },
    fresh: { data: { prose_frontier: '/docs/demo.md', prose_exists: false } },
  },
})
const LS = JSON.stringify([{ id: 'abc123def4567890', name: 'demo' }, { id: 'ffff0000eeee1111', name: null }])

const SID = '5d5c9a2e-6027-4916-bd97-eebadc8905d2'

test('ls JSON gives names, an unnamed one by its short id, and who owns each', () => {
  expect(attachments(LS)).toEqual([
    { name: 'demo', owner: false },
    { name: 'ffff0000eeee', owner: false },
  ])
  // The CLI decides ownership; only a literal true counts.
  const owned = JSON.stringify([
    { id: 'a', name: 'mine', owned: true },
    { id: 'b', name: 'theirs', owned: false },
    { id: 'c', name: 'old-cli' },
  ])
  expect(attachments(owned).map(a => a.owner)).toEqual([true, false, false])
})

test('the badge option falls back to the status line', () => {
  expect(badgeMode('band')).toBe('band')
  expect(badgeMode('off')).toBe('off')
  expect(badgeMode(undefined)).toBe('status')
  expect(badgeMode('bogus')).toBe('status')
})

test('the badge says attached with a role, none, or unknown', () => {
  expect(
    badgeText(
      [
        { name: 'a', owner: true },
        { name: 'b', owner: false },
      ],
      null,
    ),
  ).toBe('a (owner), b (helping)')
  expect(badgeText([], null)).toBe('none')
  expect(badgeText(null, 'boom')).toBe('unknown (store unreadable)')
  expect(badgeText(null, null)).toBeUndefined()
  expect(badgeText([{ name: 'a', owner: true }], 'boom')).toBe('a (owner) (stale: store unreadable)')
})

test('a frontier flattens to what the pane draws', () => {
  const f = frontierView(FRONTIER)
  expect(f.name).toBe('demo')
  expect(f.next).toEqual({ action: 'message', how: 'SendMessage to "demo-session"' })
  expect(f.sessions).toEqual([{ id: '5d5c9a2e', role: 'canonical', live: true }])
  expect(f.prose).toEqual({ path: '/docs/demo.md', exists: false })
})

test('the pane stamps the local time the frontier was read', () => {
  expect(asOfLabel('2026-11-11T00:54:06Z (16:54 -08:00)')).toBe('as of 16:54')
  expect(asOfLabel(null)).toBe('')
})

test('only commands that change attachment or captures trigger a refresh', () => {
  expect(touchesWorkstream('workstream attach demo')).toBe(true)
  expect(touchesWorkstream('cd x && workstream retire 5d5c')).toBe(true)
  expect(touchesWorkstream('~/bin/workstream frontier demo --record')).toBe(true)
  expect(touchesWorkstream('workstream frontier demo')).toBe(false)
  expect(touchesWorkstream('workstream ls --json')).toBe(false)
  expect(touchesWorkstream('rg workstream attach')).toBe(false)
})

const PANE = {
  plugin: 'workstream',
  component: 'Pane',
  requestId: 'workstream',
  props: {
    title: 'Workstream',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

test('an attached session gets the badge and a pane with its frontier', async ($, on) => {
  const clock = mock.clock(on)
  const statuses: (string | undefined)[] = []
  let opened = 0
  on('session.id', () => ({ value: '5d5c9a2e-6027-4916-bd97-eebadc8905d2' }))
  on('process.run', (_, e) => {
    const stdout = e.argv.includes('ls') ? LS.replace(',{"id":"ffff0000eeee1111","name":null}', '') : FRONTIER
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.status', (_, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.open', () => {
    opened += 1
    return { value: { isPlaced: true } }
  })
  on('ui.panes', () => ({ value: [{ id: 'workstream', title: 'Workstream', isShown: true, isFocused: false, isPlaced: true }] }))
  on('command.register', () => ({ value: { command: 'workstream-pane' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  // The start hook refreshes without blocking the session; let it land.
  await clock.settle()
  expect(statuses).toContain('demo (helping)')
  expect(opened).toBe(1)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /no recorded frontier/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /gh:o\/r#7/ })).toBeDefined()
    await ui.unmount()
  }
})

const ok = (stdout: string) => ({
  value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
})
const frontierOf = (name: string) =>
  FRONTIER.replace('"name":"demo"', `"name":"${name}"`).replace('gh:o/r#7', `gh:o/r#${name}`)

test('refresh pressed during a badge-only refresh still reads the frontier', async ($, on) => {
  const clock = mock.clock(on)
  let lsCalls = 0
  let frontierCalls = 0
  on('session.id', () => ({ value: 'sid' }))
  on('process.run', async (_, e) => {
    if (e.argv.includes('ls')) {
      lsCalls += 1
      // The first lookup is slow and finds nothing, so start opens no pane.
      if (lsCalls === 1) {
        await clock.sleep(500)
        return ok('[]')
      }
      return ok(JSON.stringify([{ id: 'x', name: 'demo' }]))
    }
    frontierCalls += 1
    return ok(FRONTIER)
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('command.register', () => ({ value: { command: 'workstream-pane' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  // A press resolves once the work it started has; that work sleeps on the mocked clock.
  const pressed = ui.press({ key: 'refresh' })
  await clock.advance(1000)
  await pressed
  expect(frontierCalls).toBe(1)
  expect(await ui.find({ type: 'Text', text: /gh:o\/r#7/ })).toBeDefined()
  await ui.unmount()
})

test('one failed frontier keeps the last good list', async ($, on) => {
  const clock = mock.clock(on)
  let failB = false
  on('session.id', () => ({ value: 'sid' }))
  on('process.run', (_, e) => {
    if (e.argv.includes('ls'))
      return ok(JSON.stringify([{ id: 'a', name: 'alpha' }, { id: 'b', name: 'beta' }]))
    if (e.argv.includes('beta') && failB)
      return { value: { exitCode: 1, stdout: '', stderr: 'gh timed out', isStdoutTruncated: false, isStderrTruncated: false } }
    return ok(frontierOf(e.argv.includes('beta') ? 'beta' : 'alpha'))
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('command.register', () => ({ value: { command: 'workstream-pane' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /gh:o\/r#beta/ })).toBeDefined()

  failB = true
  await ui.press({ key: 'refresh' })
  await clock.settle()
  expect(await ui.find({ type: 'Text', text: /gh timed out/ })).toBeDefined()
  // The next badge-only refresh clears the error; both frontiers must still be there.
  await clock.advance(60_000)
  expect(await ui.find({ type: 'Text', text: /gh:o\/r#alpha/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /gh:o\/r#beta/ })).toBeDefined()
  await ui.unmount()
})

const BAND = {
  plugin: 'workstream',
  component: 'AbovePrompt',
  requestId: 'band',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

function stubStart(on: On, ls: string, statuses: (string | undefined)[]) {
  on('session.id', () => ({ value: SID }))
  on('process.run', (_, e) => ok(e.argv.includes('ls') ? ls : FRONTIER))
  on('ui.status', (_, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  on('command.register', () => ({ value: { command: 'workstream-pane' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
}

test('badge "band" draws above the prompt and leaves the status line empty', { options: { badge: 'band' } }, async ($, on) => {
  const clock = mock.clock(on)
  const statuses: (string | undefined)[] = []
  stubStart(on, JSON.stringify([{ id: 'a', name: 'demo', owned: true }]), statuses)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(statuses.every(t => t === undefined)).toBe(true)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: 'demo' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: / · owner/ })).toBeDefined()
  await ui.unmount()
})

test('the default leaves the band to others', async ($, on) => {
  const clock = mock.clock(on)
  const statuses: (string | undefined)[] = []
  stubStart(on, LS, statuses)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(statuses).toContain('demo (helping), ffff0000eeee (helping)')
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /◆ ws/ })).toBeUndefined()
  await ui.unmount()
})
