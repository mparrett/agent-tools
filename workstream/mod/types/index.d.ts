// One workstream as the pane draws it: the parts of `workstream frontier --json`
// a person reads at a glance, flattened so the drawing needs no knowledge of
// the CLI's shapes.
export type FrontierView = {
  id: string
  name: string
  title: string
  state: string
  next: { action: string; session: string; why: string; how: string; facts: string[] } | null
  looseEnds: string[]
  anchors: { ref: string; status: string }[]
  sessions: { id: string; role: string; live: boolean }[]
  prose: { path: string; exists: boolean } | null
  asOf: string | null
}

// owner: this session holds the workstream's live canonical claim
export type Attachment = { name: string; owner: boolean }

export type WorkstreamView = {
  // null until the first lookup answers; [] means attached to none
  attached: Attachment[] | null
  frontiers: FrontierView[]
  error: string | null
  isLoading: boolean
}

declare module 'claude-code' {
  interface PluginState {
    workstream: { view: Shaped<WorkstreamView>; expanded: Shaped<string[]> }
  }
}
