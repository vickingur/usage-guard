// The guard against the engine's own test kit. The world beneath the plugin is
// answered here: a file system in memory, `sleep` that moves the mocked clock,
// the session's rate limits as the test sets them.
import { describe, expect, mock, test } from 'claude-code/testing'
import type { On, SessionRateLimit } from 'claude-code'

const DIR = '/ug'
const H = 3600
const T0 = 1_000_000_000 * 1000 // ms

type World = {
  files: Map<string, string>
  sleeps: number[]
  /** Runs before each host sleep, with how many have run: how a test changes the world mid-wait. */
  beforeSleep: (count: number) => void
  limits: SessionRateLimit[]
  clock: ReturnType<typeof mock.clock>
  write: (name: string, value: unknown) => void
  read: (name: string) => unknown
  nowSeconds: () => number
}

function world(on: On, options: { priorityEnv?: string } = {}): World {
  const files = new Map<string, string>()
  const sleeps: number[] = []
  const clock = mock.clock(on, { now: T0 })
  mock.env(on, { HOME: '/home/x', UG_DIR: DIR, ...(options.priorityEnv === undefined ? {} : { UG_PRIORITY: options.priorityEnv }) })
  const w: World = {
    files,
    sleeps,
    beforeSleep: () => {},
    limits: [],
    clock,
    write: (name, value) => files.set(`${DIR}/${name}`, JSON.stringify(value)),
    read: name => {
      const text = files.get(`${DIR}/${name}`)
      return text === undefined ? undefined : JSON.parse(text)
    },
    nowSeconds: () => clock.now() / 1000,
  }
  // Every call on `$` is an event whose bottom answers `{ value }` or `{ deny }`.
  on('fs.read', (_$, e) => {
    const text = files.get(e.path)
    return text === undefined ? { deny: `ENOENT ${e.path}` } : { value: text }
  })
  on('fs.write', (_$, e) => {
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('fs.list', (_$, e) => {
    const prefix = `${e.path}/`
    const value = [...files.keys()]
      .filter(k => k.startsWith(prefix) && !k.slice(prefix.length).includes('/'))
      .map(k => ({ name: k.slice(prefix.length), kind: 'file' as const, size: 1, mtimeMs: clock.now(), isLink: false }))
    return { value }
  })
  on('process.run', async (_$, e) => {
    if (e.argv[0] === 'sleep') {
      w.beforeSleep(sleeps.length)
      const s = Number(e.argv[1])
      sleeps.push(s)
      await clock.advance(s * 1000)
    } else if (e.argv[0] === 'rm') {
      for (const path of e.argv.slice(2)) files.delete(path)
    }
    return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.usage', () => ({ value: { startedAt: T0, context: { window: 200000 }, rateLimits: w.limits } }))
  on('session.id', () => ({ value: 'me' }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('command.register', () => ({ value: {} }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('prompt.submit', (_$, e) => ({ text: e.text, context: e.context }))
  on('tool.call', () => ({ result: 'ran' }))
  return w
}

/** 2h into a 5h window at `pct`: pace line 38, so 70 is 32 ahead. */
const fiveHour = (w: World, pct: number, resetsIn = 3 * H): SessionRateLimit[] => [
  { kind: 'five_hour', percentUsed: pct, resetsAt: new Date((w.nowSeconds() + resetsIn) * 1000).toISOString() },
]

const config = (w: World, extra: Record<string, unknown> = {}): void =>
  w.write('config.json', { poll_seconds: 5, ...extra })

const start = async ($: Parameters<Parameters<typeof test>[1] extends (a: infer A, ...r: never[]) => unknown ? A : never>[0] extends never ? never : any, w: World, limits: SessionRateLimit[]) => {
  w.limits = limits
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
}

const callBash = ($: any) => $.tool.call({ tool: 'Bash', command: 'ls' })

const total = (list: number[]): number => Math.round(list.reduce((a, b) => a + b, 0) * 100) / 100

describe('tool.call: pacing', () => {
  test('below the margin the call runs at once with no context', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 50))
    const ran = await callBash($)
    expect(ran.deny).toBe(undefined)
    expect(ran.context).toBe(undefined)
    expect(w.sleeps).toEqual([])
    expect((w.read('usage.json') as { five_hour: { used_percentage: number } }).five_hour.used_percentage).toBe(50)
  })

  test('a high session 12 over the margin waits the capped 30s in host sleeps and is told why', async ($, on) => {
    const w = world(on, { priorityEnv: 'high' })
    config(w)
    await start($, w, fiveHour(w, 70))
    const ran = await callBash($)
    expect(ran.deny).toBe(undefined)
    expect(total(w.sleeps)).toBe(30)
    expect(w.sleeps.length).toBe(6)
    expect(ran.context?.[0]).toContain('Usage guard pacing: 5h window at 70% against a pace line of 38%')
    expect(ran.context?.[0]).toContain('delayed 30s')
    expect(ran.context?.[0]).toContain('never a reason to stop')
  })

  test('a normal session beside a busy high session gets half the margin and double the delay', async ($, on) => {
    const w = world(on)
    config(w)
    w.write('sessions/other.json', { id: 'other', priority: 'high', cwd: '/o', started: 0, last_call: w.nowSeconds() - 10, updated: w.nowSeconds(), hold: null, pacing_seconds: 0, lift: 0 })
    await start($, w, fiveHour(w, 70))
    const ran = await callBash($)
    expect(total(w.sleeps)).toBe(60)
    expect(ran.context?.[0]).toContain('runs at normal priority')
  })

  test('a low session alone on the machine borrows high terms and waits like one', async ($, on) => {
    const w = world(on, { priorityEnv: 'low' })
    config(w)
    await start($, w, fiveHour(w, 70))
    const ran = await callBash($)
    expect(total(w.sleeps)).toBe(30)
    expect(ran.context?.[0]).toContain('runs at low, running as high priority')
  })

  test('a low session beside a high session that went quiet six minutes ago is halfway up the ramp', async ($, on) => {
    const w = world(on, { priorityEnv: 'low' })
    config(w)
    w.write('sessions/other.json', { id: 'other', priority: 'high', cwd: '/o', started: 0, last_call: w.nowSeconds() - 360, updated: w.nowSeconds(), hold: null, pacing_seconds: 0, lift: 0 })
    await start($, w, fiveHour(w, 70))
    const ran = await callBash($)
    // low rises whole to normal (no normal session), then halfway from normal (10, x2) to high (20, x1):
    // margin 15 -> 17 over, delay factor 1.5, cap 45 -> min(45, 17*5*1.5=127.5) = 45
    expect(total(w.sleeps)).toBe(45)
    expect(ran.context?.[0]).toContain('borrowing 50% of high')
  })

  test('`ug pace off` mid-delay releases the call within one poll', async ($, on) => {
    const w = world(on, { priorityEnv: 'high' })
    config(w)
    await start($, w, fiveHour(w, 70))
    w.beforeSleep = count => {
      if (count === 1) config(w, { pace_enabled: false })
    }
    await callBash($)
    expect(w.sleeps.length).toBe(2)
  })

  test('the registry entry records the session, its priority and the last call', async ($, on) => {
    const w = world(on, { priorityEnv: 'low' })
    config(w)
    await start($, w, fiveHour(w, 10))
    await callBash($)
    const entry = w.read('sessions/me.json') as { id: string; priority: string; last_call: number; lift: number }
    expect(entry.id).toBe('me')
    expect(entry.priority).toBe('low')
    expect(entry.last_call).toBe(w.nowSeconds())
    expect(entry.lift).toBe(2)
  })
})

describe('tool.call: the threshold hold', () => {
  test('at the threshold the call stalls and runs once `ug release` fires', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 96))
    w.beforeSleep = count => {
      if (count === 2) w.write('state.json', { release_at: w.nowSeconds() + 1 })
    }
    const ran = await callBash($)
    expect(ran.deny).toBe(undefined)
    expect(w.sleeps).toEqual([5, 5, 5])
    const entry = w.read('sessions/me.json') as { hold: null }
    expect(entry.hold).toBe(null)
  })

  test('while held the registry shows the hold, and the reset releases it', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 96, 12))
    let seen: unknown
    w.beforeSleep = () => {
      seen ??= (w.read('sessions/me.json') as { hold: unknown }).hold
    }
    await callBash($)
    expect(seen).toEqual({ label: '5h', until: w.nowSeconds(), kind: 'threshold' })
    expect(w.sleeps.length).toBeGreaterThan(1)
  })

  test('past the stall budget the call is denied with the reset time', async ($, on) => {
    const w = world(on)
    config(w, { max_stall_seconds: 12 })
    await start($, w, fiveHour(w, 96))
    const ran = await callBash($)
    expect(ran.deny).toContain('Usage guard: 5h at 96% (threshold 95%)')
    expect(ran.deny).toContain('exceeds the configured stall budget')
    expect(w.sleeps).toEqual([5, 5, 2])
  })

  test('`ug off` ends a hold', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 96))
    w.beforeSleep = () => config(w, { enabled: false })
    const ran = await callBash($)
    expect(ran.deny).toBe(undefined)
    expect(w.sleeps.length).toBe(1)
  })

  test('without usage data the guard fails open', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, [])
    const ran = await callBash($)
    expect(ran.deny).toBe(undefined)
    expect(w.sleeps).toEqual([])
  })
})

describe('priority: command and band', () => {
  const run = ($: any, args: string) =>
    $.command.run({ command: 'ug', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })

  test('/ug priority low changes the registry entry and the terms', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 70))
    w.write('sessions/other.json', { id: 'other', priority: 'normal', cwd: '/o', started: 0, last_call: w.nowSeconds(), updated: w.nowSeconds(), hold: null, pacing_seconds: 0, lift: 0 })
    const said = await run($, 'priority low')
    expect(said.text).toContain('low priority')
    expect((w.read('sessions/me.json') as { priority: string }).priority).toBe('low')
    await callBash($)
    expect(total(w.sleeps)).toBe(120) // own terms: no margin, 4x delay, cap 120
  })

  test('/ug priority with no argument cycles', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 10))
    expect((await run($, 'priority')).text).toContain('high priority')
    expect((await run($, 'priority')).text).toContain('low priority')
  })

  test('/ug answers the brief line', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 70))
    expect((await run($, '')).text).toContain('[usage] claude 5h 70% (+32 over pace line)')
  })

  test('the band shows the windows and the button cycles the priority on each surface', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, fiveHour(w, 70))
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'usage-guard', surface, component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 100, scroll: { top: 0, bodyRows: 5 }, view: {} } })
      expect(await ui.find({ type: 'Text', text: /70%/ })).toBeDefined()
      expect((await ui.find({ key: 'priority' }))?.props['label']).toContain('priority: normal')
      await ui.press({ key: 'priority' })
      expect((await ui.find({ key: 'priority' }))?.props['label']).toBe('priority: high')
      expect((w.read('sessions/me.json') as { priority: string }).priority).toBe('high')
      await ui.press({ key: 'priority' })
      expect((w.read('sessions/me.json') as { priority: string }).priority).toBe('low')
      await run($, 'priority normal')
      await ui.unmount()
    }
  })
})

describe('prompt.submit and session.measure', () => {
  test('every prompt carries the [usage] line as context', async ($, on) => {
    const w = world(on, { priorityEnv: 'high' })
    config(w)
    await start($, w, fiveHour(w, 70))
    const entered = await $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } })
    expect(entered.context?.[0]).toContain('[usage] claude 5h 70% (+32 over pace line) PACING 30s/call · priority high')
    expect(entered.context?.[0]).toContain('Spend is running ahead')
  })

  test('a measurement writes usage.json for the CLI and the other sessions', async ($, on) => {
    const w = world(on)
    config(w)
    await start($, w, [])
    expect(w.read('usage.json')).toBe(undefined)
    await $.session.measure({ context: { window: 1 }, rateLimits: fiveHour(w, 33), changed: ['rateLimits'] })
    expect((w.read('usage.json') as { five_hour: { used_percentage: number } }).five_hour.used_percentage).toBe(33)
  })
})
