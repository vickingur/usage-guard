// The usage guard as a Claude Code mod.
//
// `tool.call` is the guard: a window at or over its threshold stalls the call
// until it resets (or `ug release`, `ug off`, a raised threshold); a window
// ahead of its pace line by more than this session's margin delays the call.
// The waits run on the host (`sleep`), so they stay outside the hook's own
// ten-second budget, and every poll re-reads config and usage.
//
// Priority: each session runs at low, normal or high (the band's button, `/ug
// priority`, UG_PRIORITY, or the plugin option) and borrows an idle higher
// class's terms progressively; sessions/<id>.json under the guard's directory
// is how sessions on this machine see each other.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { UsageGuardView, UsageGuardWindow } from '../types'
import {
  ACCOUNT_TERMS,
  type Config,
  type Hold,
  type Pace,
  type Priority,
  type SessionEntry,
  type Terms,
  type Usage,
  type Violation,
  WINDOWS,
  fmtClock,
  fresher,
  fmtDuration,
  idleAbove,
  isPriority,
  isStale,
  nextPriority,
  num,
  paceDelay,
  mergeUsage,
  paces,
  parseConfig,
  sessionFromFile,
  terms,
  usageFromFile,
  usageFromSession,
  usageToFile,
  violations,
} from './pace'

const priorityAtom = atom({ plugin: 'usage-guard', key: 'priority' } as const, 'normal')
const viewAtom = atom({ plugin: 'usage-guard', key: 'view' } as const, null)

// --- disk ---
// Everything the guard keeps on disk, through `$`; it must sit in this file: the
// validator follows `$` only into functions declared beside the hooks. All of it lives under one
// node-local directory (UG_DIR, else ~/.claude/usage-guard) that the `ug` CLI
// and the Codex hooks share:
//
//   config.json          settings (ug writes, the mod reads)
//   usage.json           the account's Claude windows, as this mod last saw them
//   codex-usage.json     Codex's windows, written by the Codex hooks
//   state.json           `release_at`, written by `ug release`
//   sessions/<id>.json   one entry per live Claude session: priority, last call, hold

type Paths = {
  dir: string
  config: string
  usage: string
  codexUsage: string
  state: string
  sessions: string
}

let cached: Paths | undefined

async function paths($: EngineInterface): Promise<Paths> {
  if (cached !== undefined) return cached
  const override = await $.env.get('UG_DIR')
  const home = await $.env.get('HOME')
  if (override === undefined && home === undefined) throw new Error('usage-guard: neither UG_DIR nor HOME is set')
  const dir = override ?? `${home}/.claude/usage-guard`
  cached = {
    dir,
    config: `${dir}/config.json`,
    usage: `${dir}/usage.json`,
    codexUsage: `${dir}/codex-usage.json`,
    state: `${dir}/state.json`,
    sessions: `${dir}/sessions`,
  }
  return cached
}

/** The parsed file, or undefined when it is missing or not JSON. */
async function readJson($: EngineInterface, path: string): Promise<unknown> {
  let text: string
  try {
    const got = await $.fs.read(path)
    if (typeof got !== 'string') return undefined
    text = got
  } catch {
    return undefined
  }
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

const writeJson = ($: EngineInterface, path: string, value: unknown): Promise<void> =>
  $.fs.write(path, `${JSON.stringify(value, null, 2)}\n`)

const loadConfig = async ($: EngineInterface, p: Paths): Promise<Config> =>
  parseConfig(await readJson($, p.config))

// --- usage -------------------------------------------------------------------

const readUsage = async ($: EngineInterface, p: Paths, file: 'usage' | 'codexUsage'): Promise<Usage | undefined> =>
  usageFromFile(await readJson($, p[file]))

async function writeUsage($: EngineInterface, p: Paths, usage: Usage, now: number): Promise<Usage> {
  const merged = mergeUsage(usage, await readUsage($, p, 'usage'), now)
  await writeJson($, p.usage, usageToFile(merged))
  return merged
}

// --- the session registry -----------------------------------------------------

const sessionFile = (p: Paths, id: string): string => `${p.sessions}/${id}.json`

const writeSession = ($: EngineInterface, p: Paths, entry: SessionEntry): Promise<void> =>
  writeJson($, sessionFile(p, entry.id), entry)

/** Every live session's entry; one not updated within `session_stale_seconds` is ignored. */
async function readSessions($: EngineInterface, p: Paths, cfg: Config, now: number): Promise<SessionEntry[]> {
  let names: string[]
  try {
    names = (await $.fs.list(p.sessions)).filter(e => e.kind === 'file' && e.name.endsWith('.json')).map(e => e.name)
  } catch {
    return []
  }
  const docs = await Promise.all(names.map(name => readJson($, `${p.sessions}/${name}`)))
  const out: SessionEntry[] = []
  for (const doc of docs) {
    const entry = sessionFromFile(doc)
    if (entry !== undefined && now - entry.updated <= cfg.session_stale_seconds) out.push(entry)
  }
  return out
}

/** Removes session files not updated for a day, and this session's own when asked. */
async function pruneSessions($: EngineInterface, p: Paths, now: number, alsoId?: string): Promise<void> {
  let entries: { name: string; mtimeMs: number }[]
  try {
    entries = await $.fs.list(p.sessions)
  } catch {
    return
  }
  const stale = entries
    .filter(e => e.name.endsWith('.json') && (now - e.mtimeMs / 1000 > 86400 || (alsoId !== undefined && e.name === `${alsoId}.json`)))
    .map(e => `${p.sessions}/${e.name}`)
  if (stale.length > 0) await $.process.run(['rm', '-f', ...stale], { timeoutMs: 5000 })
}

/** True when `ug release` ran after `start` (epoch seconds). */
async function releasedSince($: EngineInterface, p: Paths, start: number): Promise<boolean> {
  const doc = await readJson($, p.state)
  if (typeof doc !== 'object' || doc === null) return false
  const at = num((doc as Record<string, unknown>)['release_at'])
  return at !== undefined && at > start
}

/**
 * Waits on the host, outside the hook's own time: a hook has ten seconds of
 * its own per dispatch and `$.clock.sleep` spends them, while a `$` call in
 * flight does not count. `sleep(1)` exists on every macOS and Linux.
 */
async function hostSleep($: EngineInterface, seconds: number, signal: AbortSignal): Promise<void> {
  const s = Math.min(Math.max(seconds, 0.05), 590)
  if (signal.aborted) return
  await $.process.run(['sleep', s.toFixed(2)], { timeoutMs: Math.ceil(s * 1000) + 5000 })
}

const REGISTRY_TTL_SECONDS = 5
const HEARTBEAT_SECONDS = 10
const USAGE_FILE_MAX_AGE_SECONDS = 60

const ADVICE =
  'Keep working through the delays: prefer fewer, larger steps, batch reads, and defer fan-outs and long loops ' +
  'until back on pace. Pacing is never a reason to stop, pause or ask the user to continue. `ug status --json` has the numbers.'

/** This session, as the module tracks it between dispatches. Rebuilt by session.start on every (re)load. */
type Live = {
  id: string
  cwd: string
  started: number
  priority: Priority
  lastCall: number
  lastMeasureAt: number
  lastWrite: number
  hold: Hold | null
  pacingSeconds: number
  lift: number
  registry: { at: number; entries: SessionEntry[] } | undefined
}

type Verdict = { deny?: string; context?: string }

// Module state: lost on a hot reload and rebuilt by session.start, which fires again then.
let live: Live | undefined
let lastViewJson = ''
let defaultPriority: Priority = 'normal'
// Why a tool call being held would be refused if this hook were lost mid-hold
// (its .catch answers in its place); absent while pacing or idle, so a fault
// there lets the call through.
const holdReasons = new Map<string, string>()

const seconds = async ($: EngineInterface): Promise<number> => (await $.clock.now()) / 1000

const session = (): Live => {
  if (live === undefined) throw new Error('usage-guard: session.start has not run')
  return live
}

// --- disk ---------------------------------------------------------------

const touch = async ($: EngineInterface, p: Paths, now: number, force: boolean): Promise<void> => {
  const s = session()
  if (!force && now - s.lastWrite < HEARTBEAT_SECONDS) return
  s.lastWrite = now
  await writeSession($, p, {
    id: s.id,
    priority: s.priority,
    cwd: s.cwd,
    started: s.started,
    last_call: s.lastCall,
    updated: now,
    hold: s.hold,
    pacing_seconds: s.pacingSeconds,
    lift: s.lift,
  })
}

/** The freshest reading: this session's own, stamped when it last moved, or usage.json. */
const currentUsage = async ($: EngineInterface, p: Paths, now: number, ownAt: number): Promise<Usage | undefined> => {
  const own = usageFromSession((await $.session.usage()).rateLimits, ownAt)
  const file = await readUsage($, p, 'usage')
  if (own !== undefined && (file === undefined || own.ts > file.ts)) {
    if (file === undefined || now - file.ts > USAGE_FILE_MAX_AGE_SECONDS) return writeUsage($, p, own, now)
    return own
  }
  return file
}

const sessionTerms = async ($: EngineInterface, p: Paths, cfg: Config, now: number): Promise<Terms> => {
  const s = session()
  if (s.registry === undefined || now - s.registry.at > REGISTRY_TTL_SECONDS) {
    s.registry = { at: now, entries: await readSessions($, p, cfg, now) }
  }
  const t = terms(s.priority, idleAbove(s.registry.entries, s.id, now), cfg)
  s.lift = t.lift
  return t
}

// --- what the band, the status line and the brief show ---------------------

const windowsView = (list: readonly Pace[], now: number): UsageGuardWindow[] =>
  list.map(p => ({
    label: p.label,
    pct: p.pct,
    threshold: p.threshold,
    ahead: p.ahead,
    pacing: p.active,
    delaySeconds: p.delaySeconds,
    resetsIn: Math.max(0, p.resetsAt - now),
  }))

const publish = async (
  $: EngineInterface,
  p: Paths,
  cfg: Config,
  usage: Usage | undefined,
  now: number,
  t: Terms,
): Promise<readonly Pace[]> => {
  const s = session()
  const stale = isStale(usage, cfg, now)
  const list = stale ? [] : paces(usage, cfg, now, t)
  const codex = await readUsage($, p, 'codexUsage')
  const active = list.filter(one => one.active)
  const worst = active.reduce<Pace | undefined>((a, b) => (a === undefined || b.ahead - b.margin > a.ahead - a.margin ? b : a), undefined)
  const view: UsageGuardView = {
    windows: windowsView(list, now),
    codex: (codex === undefined || now - codex.ts > cfg.codex_log_max_age_seconds ? [] : codex.windows)
      .filter(r => r.resetsAt > now)
      .map(r => ({ label: WINDOWS.find(w => w.key === r.key)?.label ?? r.key, pct: r.pct })),
    hold: s.hold,
    pacing: worst === undefined ? null : { delaySeconds: paceDelay(active), backIn: Math.max(0, worst.catchupAt - now) },
    lift: s.lift,
    stale,
    enabled: cfg.enabled,
    paceEnabled: cfg.pace_enabled,
  }
  const json = JSON.stringify(view)
  if (json !== lastViewJson) {
    lastViewJson = json
    await update($, viewAtom, () => view)
  }
  $.ui.status(statusText(view))
  return list
}

const statusText = (view: UsageGuardView): string | undefined => {
  if (view.hold !== null) {
    const at = view.hold.until
    return `usage guard: HOLD ${view.hold.label} until ${fmtClock(at)}`
  }
  if (view.pacing !== null && view.pacing.delaySeconds > 0) {
    const ahead = view.windows.filter(w => w.pacing).map(w => `${w.label} +${Math.round(w.ahead)}`).join(', ')
    return `usage guard: pacing ${Math.round(view.pacing.delaySeconds)}s/call (${ahead} over pace line)`
  }
  return undefined
}

const liftText = (priority: Priority, lift: number): string => {
  if (lift < 0.05) return priority
  const above = nextPriority(priority)
  const whole = Math.floor(lift + 1e-9)
  const partial = lift - whole
  if (whole >= 1 && partial < 0.05) return `${priority}, running as ${whole === 2 ? 'high' : above}`
  const target = whole >= 1 ? 'high' : above
  return `${priority}, borrowing ${Math.round(partial * 100)}% of ${target}`
}

const windowPhrase = (w: UsageGuardWindow): string => {
  let text = `${w.label} ${Math.round(w.pct)}%`
  if (w.ahead >= 0.5) text += ` (+${Math.round(w.ahead)} over pace line)`
  if (w.pacing) text += ` PACING ${Math.round(w.delaySeconds)}s/call`
  return text
}

const briefLine = (view: UsageGuardView, priority: Priority): string => {
  const parts: string[] = []
  if (view.windows.length > 0) parts.push(`claude ${view.windows.map(windowPhrase).join(', ')}`)
  else if (view.stale) parts.push('claude: usage data stale')
  if (view.codex.length > 0) parts.push(`codex ${view.codex.map(w => `${w.label} ${Math.round(w.pct)}%`).join(', ')}`)
  if (view.hold !== null) parts.push(`HOLD on ${view.hold.label} until ${fmtClock(view.hold.until)}`)
  if (parts.length === 0) return ''
  parts.push(`priority ${liftText(priority, view.lift)}`)
  let line = `[usage] ${parts.join(' · ')}`
  if (!view.enabled) line += ' · guard off'
  else if (!view.paceEnabled) line += ' · pacing off'
  if (view.windows.some(w => w.pacing)) line += `. Spend is running ahead of the window: the guard is pacing each tool call for you. ${ADVICE}`
  return line
}

const paceContext = (entered: readonly Pace[], t: Terms, cfg: Config, waited: number, now: number): string | undefined => {
  const active = entered.filter(p => p.active)
  if (active.length === 0) return undefined
  const parts = active.map(
    p => `${p.label} window at ${Math.round(p.pct)}% against a pace line of ${Math.round(p.line)}% (+${Math.round(p.ahead)}, margin ${Math.round(p.margin)})`,
  )
  const worst = active.reduce((a, b) => (b.ahead - b.margin > a.ahead - a.margin ? b : a))
  const how =
    cfg.pace_mode === 'delay'
      ? `each tool call is being delayed ${Math.round(paceDelay(active))}s`
      : `this call was held ${fmtDuration(waited)}`
  const s = session()
  return (
    `Usage guard pacing: ${parts.join('; ')}. This session runs at ${liftText(s.priority, t.lift)} priority, so ${how}; ` +
    `back on pace in about ${fmtDuration(worst.catchupAt - now)} at the current rate. ${ADVICE}`
  )
}

const denyText = (current: readonly Violation[], worst: Violation, waited: number): string => {
  const labels = current.map(v => `${v.label} at ${Math.round(v.pct)}%`).join(', ')
  const d = new Date(worst.resetsAt * 1000)
  const clock = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${fmtClock(worst.resetsAt)}`
  return (
    `Usage guard: ${labels} (threshold ${Math.round(worst.threshold)}%). Held for ${fmtDuration(waited)} and the window ` +
    `does not reset until ${clock}, which exceeds the configured stall budget. Stop and wait, or run \`ug off\` to disable the guard.`
  )
}

// --- the guard ------------------------------------------------------------

const guard = async ($: EngineInterface, toolUseId: string, signal: AbortSignal): Promise<Verdict> => {
  const p = await paths($)
  const s = session()
  let cfg = await loadConfig($, p)
  let now = await seconds($)
  s.lastCall = now
  s.lastMeasureAt = now // a tool call follows a model response: the engine's figures are this fresh
  const t = await sessionTerms($, p, cfg, now)
  await touch($, p, now, false)
  if (!cfg.enabled) {
    await publish($, p, cfg, undefined, now, ACCOUNT_TERMS)
    return {}
  }
  let usage = await currentUsage($, p, now, now)
  if (isStale(usage, cfg, now)) {
    await publish($, p, cfg, undefined, now, ACCOUNT_TERMS)
    return {} // fail open: no trustworthy data means no hold
  }

  const clearHold = async (): Promise<void> => {
    holdReasons.delete(toolUseId)
    if (s.hold === null) return
    s.hold = null
    s.pacingSeconds = 0
    await touch($, p, now, true)
  }

  let current = violations(usage, cfg, now)
  if (current.length > 0) {
    const start = now
    const deadline = start + cfg.max_stall_seconds
    for (;;) {
      const worst = current.reduce((a, b) => (b.resetsAt > a.resetsAt ? b : a))
      s.hold = { label: current.map(v => v.label).join('+'), until: worst.resetsAt, kind: 'threshold' }
      const reason = denyText(current, worst, now - start)
      holdReasons.set(toolUseId, reason)
      await touch($, p, now, true)
      await publish($, p, cfg, usage, now, ACCOUNT_TERMS)
      if (now >= deadline) {
        await clearHold()
        return { deny: reason }
      }
      await hostSleep($, Math.min(cfg.poll_seconds, deadline - now, worst.resetsAt - now), signal)
      if (signal.aborted) {
        await clearHold()
        return {}
      }
      now = await seconds($)
      cfg = await loadConfig($, p)
      if (!cfg.enabled || (await releasedSince($, p, start))) break
      usage = await currentUsage($, p, now, s.lastMeasureAt)
      if (isStale(usage, cfg, now)) break
      current = violations(usage, cfg, now)
      if (current.length === 0) break
    }
    await clearHold()
    await publish($, p, cfg, usage, now, ACCOUNT_TERMS)
    return {}
  }

  let list = await publish($, p, cfg, usage, now, t)
  if (!list.some(one => one.active)) return {}
  const entered = list
  const start = now

  if (cfg.pace_mode === 'delay') {
    const until = start + paceDelay(list)
    s.pacingSeconds = until - start
    await touch($, p, now, true)
    while (now < until) {
      await hostSleep($, Math.min(cfg.poll_seconds, until - now), signal)
      if (signal.aborted) break
      now = await seconds($)
      cfg = await loadConfig($, p)
      if (!cfg.enabled || !cfg.pace_enabled || (await releasedSince($, p, start))) break
    }
    s.pacingSeconds = 0
    return { context: paceContext(entered, t, cfg, now - start, now) }
  }

  const deadline = start + cfg.max_stall_seconds
  for (;;) {
    const active = list.filter(one => one.active)
    if (active.length === 0 || now >= deadline) break
    const worst = active.reduce((a, b) => (b.catchupAt > a.catchupAt ? b : a))
    s.hold = { label: `pace ${active.map(one => one.label).join('+')}`, until: worst.catchupAt, kind: 'pace' }
    await touch($, p, now, true)
    await publish($, p, cfg, usage, now, t)
    await hostSleep($, Math.min(cfg.poll_seconds, deadline - now, worst.catchupAt - now), signal)
    if (signal.aborted) {
      await clearHold()
      return {}
    }
    now = await seconds($)
    cfg = await loadConfig($, p)
    if (!cfg.enabled || !cfg.pace_enabled || (await releasedSince($, p, start))) break
    usage = await currentUsage($, p, now, s.lastMeasureAt)
    if (isStale(usage, cfg, now)) break
    list = paces(usage, cfg, now, t)
  }
  await clearHold()
  await publish($, p, cfg, usage, now, t)
  return { context: paceContext(entered, t, cfg, now - start, now) }
}

// --- priority ---

const setPriority = async ($: EngineInterface, priority: Priority): Promise<void> => {
  const s = session()
  const p = await paths($)
  const now = await seconds($)
  s.priority = priority
  s.registry = undefined
  await update($, priorityAtom, () => priority)
  await touch($, p, now, true)
  const cfg = await loadConfig($, p)
  const t = await sessionTerms($, p, cfg, now)
  await publish($, p, cfg, await currentUsage($, p, now, s.lastMeasureAt), now, t)
}

const cyclePriority = async ($: EngineInterface): Promise<Priority> => {
  const priority = nextPriority(session().priority)
  await setPriority($, priority)
  $.ui.toast(`priority: ${priority}`)
  return priority
}

export const register: Register = (on, options) => {
  defaultPriority = isPriority(options['priority']) ? options['priority'] : 'normal'

  on('tool.call', async ($, e, next) => {
    const verdict = await guard($, e.tool_use_id, next.signal)
    if (verdict.deny !== undefined) return { deny: verdict.deny }
    const ran = await next(e)
    if (verdict.context === undefined || ran.deny !== undefined) return ran
    return { ...ran, context: [...(ran.context ?? []), verdict.context] }
  }).catch(($, e, next) => {
    if (next.called) return next(e)
    const reason = holdReasons.get(e.tool_use_id)
    holdReasons.delete(e.tool_use_id)
    return reason === undefined ? next(e) : { deny: reason }
  })

  // --- the band above the prompt ---

  const pctColor = (pct: number, threshold: number): 'error' | 'warning' | 'success' =>
    pct >= threshold ? 'error' : pct >= threshold * 0.66 ? 'warning' : 'success'

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const view = await read($, viewAtom)
    const priority = await read($, priorityAtom)
    const { Box, Button, Text } = $.ui.resolve(e)
    const hold = view?.hold ?? null
    const pacing = view?.pacing ?? null
    return (
      <Box flexDirection="row" gap={1}>
        <Text dimColor>usage</Text>
        {view === null || (view.windows.length === 0 && !view.stale) ? (
          <Text dimColor>no data yet</Text>
        ) : view.windows.length === 0 ? (
          <Text color="warning">stale</Text>
        ) : (
          view.windows.map(w => (
            <Box gap={0}>
              <Text dimColor>{w.label} </Text>
              <Text color={pctColor(w.pct, w.threshold)}>{Math.round(w.pct)}%</Text>
              {w.ahead >= 0.5 && (
                <Text color={w.pacing ? 'warning' : undefined} dimColor={!w.pacing} bold={w.pacing}>
                  {' '}+{Math.round(w.ahead)}
                  {w.pacing ? '▲' : ''}
                </Text>
              )}
            </Box>
          ))
        )}
        {view !== null && view.codex.length > 0 && (
          <Text dimColor>cx {view.codex.map(w => `${w.label} ${Math.round(w.pct)}%`).join(' ')}</Text>
        )}
        {hold !== null && (
          <Text color={hold.kind === 'pace' ? 'warning' : 'error'} bold inverse>
            {' '}HOLD {hold.label} until {fmtClock(hold.until)}{' '}
          </Text>
        )}
        {hold === null && pacing !== null && pacing.delaySeconds > 0 && (
          <Text color="warning" bold>
            pacing {Math.round(pacing.delaySeconds)}s/call, back in {fmtDuration(pacing.backIn)}
          </Text>
        )}
        {view !== null && !view.enabled && <Text color="warning">guard off</Text>}
        {view !== null && view.enabled && !view.paceEnabled && <Text color="warning">pace off</Text>}
        <Button key="priority" hotkey="p" label={`priority: ${liftText(priority, view?.lift ?? 0)}`} onPress={() => void cyclePriority($)} />
      </Box>
    )
  })

  // --- /ug ----------------------------------------------------------------------

  on('command.run', { command: 'ug' }, async ($, e) => {
    const words = e.args.trim().split(/\s+/).filter(Boolean)
    const s = session()
    if (words[0] === 'priority') {
      const wanted = words[1]
      if (wanted === undefined) {
        const priority = await cyclePriority($)
        return { text: `usage guard: this session now runs at ${priority} priority` }
      }
      if (!isPriority(wanted)) return { text: 'usage: /ug priority [low|normal|high]' }
      await setPriority($, wanted)
      return { text: `usage guard: this session now runs at ${wanted} priority` }
    }
    const view = await read($, viewAtom)
    const line = view === null ? 'no usage data yet' : briefLine(view, s.priority) || 'no usage data yet'
    return { text: `${line}\n\nug status --json has every number; ug priority, ug off, ug release and ug pace change the guard.` }
  })

  // --- the session ----------------------------------------------------------------

  on('prompt.submit', async ($, e, next) => {
    const s = session()
    const p = await paths($)
    const cfg = await loadConfig($, p)
    const now = await seconds($)
    const t = await sessionTerms($, p, cfg, now)
    const view = await (async () => {
      await publish($, p, cfg, await currentUsage($, p, now, s.lastMeasureAt), now, t)
      return read($, viewAtom)
    })()
    const line = view === null ? '' : briefLine(view, s.priority)
    return next(line === '' ? e : { ...e, context: [...(e.context ?? []), line] })
  })

  on('session.measure', async ($, e, next) => {
    if (live !== undefined && e.changed.includes('rateLimits')) {
      const p = await paths($)
      const cfg = await loadConfig($, p)
      const now = await seconds($)
      const own = usageFromSession(e.rateLimits, now)
      if (own !== undefined) {
        live.lastMeasureAt = now
        const merged = await writeUsage($, p, own, now)
        const t = await sessionTerms($, p, cfg, now)
        await publish($, p, cfg, merged, now, t)
      }
    }
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    const p = await paths($)
    const cfg = await loadConfig($, p)
    const now = await seconds($)
    const fromEnv = await $.env.get('UG_PRIORITY')
    const priority: Priority = isPriority(fromEnv) ? fromEnv : defaultPriority
    live = {
      id: await $.session.id(),
      cwd: e.cwd,
      started: now,
      priority,
      lastCall: now,
      lastMeasureAt: 0,
      lastWrite: 0,
      hold: null,
      pacingSeconds: 0,
      lift: 0,
      registry: undefined,
    }
    lastViewJson = ''
    await update($, priorityAtom, () => priority)
    await $.command.register({
      name: 'ug',
      description: "Usage guard: where the windows stand, or set this session's priority",
      argumentHint: '[priority [low|normal|high]]',
    })
    await pruneSessions($, p, now)
    const t = await sessionTerms($, p, cfg, now)
    await touch($, p, now, true)
    const own = usageFromSession((await $.session.usage()).rateLimits, now)
    if (own !== undefined) {
      live.lastMeasureAt = now
      await writeUsage($, p, own, now)
    }
    await publish($, p, cfg, await currentUsage($, p, now, live.lastMeasureAt), now, t)
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    if (live !== undefined && e.reason !== 'clear') {
      const p = await paths($)
      await pruneSessions($, p, await seconds($), live.id)
    }
    return next(e)
  })
}
