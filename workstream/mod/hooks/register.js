import { atom, read, update } from 'claude-code';
import { asOfLabel, attachments, badgeMode, badgeText, frontierView, roleLabel, touchesWorkstream } from './model';
const PANE = 'workstream';
const COMMAND = 'workstream-pane';
// `ls --session` is under a second, so the badge can keep close to the store;
// a frontier probes every anchor through gh and git (seconds per workstream),
// so the pane refreshes far less often, and only while it is open.
const BADGE_EVERY_MS = 60000;
const FRONTIER_EVERY_MS = 5 * 60000;
const INITIAL = { attached: null, frontiers: [], error: null, isLoading: false };
// Change the tag when WorkstreamView's fields change: a value outlives a reload.
const view = atom({ plugin: 'workstream', key: 'view' }, INITIAL, { shape: 'view-2' });
// Workstreams whose next-step instructions are unfolded in the pane, by name.
const expanded = atom({ plugin: 'workstream', key: 'expanded' }, [], { shape: 'expanded-1' });
// Where the badge goes, from the `badge` option. Options are fixed for an
// activation; a change reloads the plugin and runs `register` again.
let mode = 'status';
// The CLI beside this mod, not one found on PATH: a session started without an
// interactive parent shell may not have ~/bin on PATH, and this copy always
// matches the hooks the plugin ships.
async function cli($, args) {
    const bin = `${$.plugin.root}/../../bin/workstream`;
    const r = await $.process.run([bin, ...args], { timeoutMs: 60000 });
    if (r.exitCode !== 0) {
        const last = r.stderr.trim().split('\n').pop();
        throw new Error(last || `workstream ${args[0]} exited ${r.exitCode}`);
    }
    return r.stdout;
}
async function isPaneOpen($) {
    return (await $.ui.panes()).some(p => p.id === PANE);
}
async function refresh($, withFrontier) {
    await update($, view, v => ({ ...v, isLoading: true }));
    let attached = null;
    let frontiers;
    let error = null;
    try {
        const sid = await $.session.id();
        attached = attachments(await cli($, ['ls', '--session', sid, '--json']));
        if (withFrontier) {
            // Built aside and kept only whole: one failed frontier keeps the last good list.
            const fresh = [];
            for (const { name } of attached) {
                fresh.push(frontierView(await cli($, ['frontier', name, '--json'])));
            }
            frontiers = fresh;
        }
    }
    catch (err) {
        error = err instanceof Error ? err.message : String(err);
    }
    const next = await update($, view, v => ({
        attached: attached ?? v.attached,
        frontiers: frontiers ?? v.frontiers,
        error,
        isLoading: false,
    }));
    // The engine draws every plugin status line as a pinned warning (⚠, yellow);
    // the band is the opt-in for a quieter badge. The band redraws on the write above.
    $.ui.status(mode === 'status' ? badgeText(next.attached, next.error) : undefined);
}
// One refresh at a time: a timer firing while a slow frontier runs would
// otherwise start a second one against the same store. A module variable is
// enough: a reload drops the timers that could overlap with it.
let running = null;
let runningHasFrontier = false;
let timers = [];
function kick($, withFrontier) {
    if (running && (runningHasFrontier || !withFrontier))
        return running;
    // A frontier asked for while a badge-only refresh runs is queued after it,
    // not folded into it: the person pressed refresh or opened the pane.
    const mine = (running ?? Promise.resolve())
        .catch(() => undefined)
        .then(() => refresh($, withFrontier))
        .finally(() => {
        if (running === mine)
            running = null;
    });
    running = mine;
    runningHasFrontier = withFrontier;
    return mine;
}
export const register = (on, options) => {
    mode = badgeMode(options.badge);
    on('session.start', async ($, e, next) => {
        await $.command.register({
            name: COMMAND,
            description: "Open the workstream pane: this session's workstreams and their frontier",
        });
        // Whatever started before is stopped first, so a second start never doubles the polling.
        for (const t of timers)
            t.cancel();
        timers = [
            $.clock.every(BADGE_EVERY_MS, () => void kick($, false)),
            $.clock.every(FRONTIER_EVERY_MS, async () => {
                if (await isPaneOpen($))
                    void kick($, true);
            }),
        ];
        // Not awaited: the first prompt need not wait on the store.
        void kick($, false).then(async () => {
            const { attached } = await read($, view);
            // Opened unasked only for an attached session; the surface seats an
            // unasked pane only where it fits beside the transcript.
            if (attached?.length) {
                await $.ui.open({ id: PANE, title: 'Workstream' });
                void kick($, true);
            }
        });
        return next(e);
    });
    on('command.run', { command: COMMAND }, async ($) => {
        await $.ui.open({ id: PANE, title: 'Workstream' });
        void kick($, true);
        return { text: 'Workstream pane opened.' };
    }).catch(() => ({ text: 'The workstream pane could not open; see claude --debug.' }));
    // Never a gate: a failed refresh must not refuse the command it watched.
    on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
        const ran = await next(e);
        if (touchesWorkstream(e.command))
            void kick($, await isPaneOpen($));
        return ran;
    }).catch(($, e, next) => next(e));
    // Two colors: dim chrome and roles, the names in the theme's suggestion color.
    // Store errors are the one thing drawn as a warning.
    on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
        if (mode !== 'band' || e.props.hasSurvey)
            return next(e);
        const v = await read($, view);
        if (v.attached === null && !v.error)
            return next(e);
        const { Box, Text } = $.ui.resolve(e);
        return (<Box flexDirection="row">
        <Text color="inactive">◆ ws </Text>
        {v.attached === null ? (<Text color="warning">unknown (store unreadable)</Text>) : v.attached.length === 0 ? (<Text color="inactive">none</Text>) : (v.attached.map((a, i) => (<Text>
              {i > 0 ? <Text color="inactive">, </Text> : null}
              <Text color="suggestion">{a.name}</Text>
              <Text color="inactive"> · {roleLabel(a)}</Text>
            </Text>)))}
        {v.attached !== null && v.error ? <Text color="warning"> (stale: store unreadable)</Text> : null}
      </Box>);
    });
    on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
        const { Box, Text, Button } = $.ui.resolve(e);
        const v = await read($, view);
        const open = await read($, expanded);
        const toggle = (name) => update($, expanded, xs => (xs.includes(name) ? xs.filter(x => x !== name) : [...xs, name]));
        const header = (<Box flexDirection="row" justifyContent="space-between">
        <Text dimColor>{v.isLoading ? 'refreshing…' : asOfLabel(v.frontiers[0]?.asOf ?? null)}</Text>
        <Button key="refresh" label="refresh" onPress={() => kick($, true)}/>
      </Box>);
        if (v.error) {
            return (<Box flexDirection="column">
          {header}
          <Text color="error">{v.error}</Text>
        </Box>);
        }
        if (v.attached === null || (v.attached.length > 0 && v.frontiers.length === 0)) {
            return (<Box flexDirection="column">
          {header}
          <Text dimColor>Reading the workstream store…</Text>
        </Box>);
        }
        if (v.attached.length === 0) {
            return (<Box flexDirection="column">
          {header}
          <Text>This session is attached to no workstream.</Text>
          <Text dimColor>workstream attach NAME, or --new "TITLE" --name SLUG</Text>
        </Box>);
        }
        return (<Box flexDirection="column">
        {header}
        {v.frontiers.map(f => (<Box flexDirection="column" marginTop={1}>
            <Text>
              <Text weight="semibold">{f.name}</Text> <Text dimColor>[{f.state}]</Text>
            </Text>
            <Text>{f.title}</Text>
            {f.next && (<Box flexDirection="row" justifyContent="space-between">
                <Text>
                  <Text color="suggestion">
                    next: {f.next.action}
                    {f.next.session ? ` ${f.next.session}` : ''}
                  </Text>
                  {f.next.why ? <Text dimColor> · {f.next.why}</Text> : null}
                </Text>
                {f.next.how || f.next.facts.length ? (<Button key={`how:${f.name}`} label={open.includes(f.name) ? 'how ▾' : 'how ▸'} onPress={() => toggle(f.name)}/>) : null}
              </Box>)}
            {f.next && open.includes(f.name) && (<Box flexDirection="column">
                {f.next.how ? <Text dimColor>  {f.next.how}</Text> : null}
                {f.next.facts.map(x => (<Text dimColor>  • {x}</Text>))}
              </Box>)}
            {f.looseEnds.length > 0 && (<Box flexDirection="column" marginTop={1}>
                <Text weight="semibold">loose ends</Text>
                {f.looseEnds.map(l => (<Text color="warning">  • {l}</Text>))}
              </Box>)}
            {f.anchors.length > 0 && (<Box flexDirection="column" marginTop={1}>
                <Text weight="semibold">anchors</Text>
                {f.anchors.map(a => (<Text>
                    {'  '}
                    <Text dimColor>{a.status.padEnd(9)}</Text>
                    {a.ref}
                  </Text>))}
              </Box>)}
            <Box flexDirection="column" marginTop={1}>
              <Text weight="semibold">sessions</Text>
              {f.sessions.map(s => (<Text>
                  {'  '}
                  {s.id} <Text dimColor>{s.role}</Text>
                  {s.live ? <Text color="success"> live</Text> : null}
                </Text>))}
            </Box>
            {f.prose && (<Text dimColor>
                prose: {f.prose.path}
                {f.prose.exists ? '' : ' (not written yet)'}
              </Text>)}
          </Box>))}
      </Box>);
    });
};
