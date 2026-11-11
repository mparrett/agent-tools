import { expect, mock, test } from 'claude-code/testing'

import { asOfLabel, attachedNames, badgeText, frontierView, touchesWorkstream } from './model'

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

test('ls JSON gives names, an unnamed one by its short id', () => {
  expect(attachedNames(LS)).toEqual(['demo', 'ffff0000eeee'])
})

test('the badge says attached, none, or unknown', () => {
  expect(badgeText(['a', 'b'], null)).toBe('a, b')
  expect(badgeText([], null)).toBe('none')
  expect(badgeText(null, 'boom')).toBe('unknown (store unreadable)')
  expect(badgeText(null, null)).toBeUndefined()
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
  expect(statuses).toContain('demo')
  expect(opened).toBe(1)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /no recorded frontier/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /gh:o\/r#7/ })).toBeDefined()
    await ui.unmount()
  }
})
