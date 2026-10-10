import { describe, expect, test } from 'claude-code/testing'

import { CASES } from './pace-cases'
import {
  type Config,
  type Priority,
  type SessionEntry,
  WINDOWS,
  fmtDuration,
  holdLevel,
  idleAbove,
  mergeUsage,
  nextPriority,
  lineRateAt,
  paceLine,
  paces,
  parseConfig,
  profileOf,
  ramp,
  sessionFromFile,
  terms,
  usageFromFile,
  usageFromSession,
  usageToFile,
  violations,
} from './pace'

const round = (n: number): number => Math.round(n * 1000) / 1000

describe('parity cases (hooks/pace-cases.ts, shared with tests/test_parity.py)', () => {
  for (const c of CASES) {
    test(c.name, () => {
      const cfg = parseConfig(c.config)
      const t = terms(c.priority as Priority, c.idle as Partial<Record<Priority, number>>, cfg)
      expect(round(t.marginFactor)).toBe(c.expect.terms.marginFactor)
      expect(round(t.delayFactor)).toBe(c.expect.terms.delayFactor)
      expect(round(t.lift)).toBe(c.expect.terms.lift)
      const usage = { ts: c.now, windows: c.windows.map(w => ({ key: w.key as 'five_hour' | 'seven_day', pct: w.pct, resetsAt: c.now + w.resets_in })) }
      const got = paces(usage, cfg, c.now, t, profileOf(cfg, 0))
      expect(got.length).toBe(c.expect.paces.length)
      got.forEach((p, i) => {
        const want = c.expect.paces[i]
        if (want === undefined) throw new Error('missing expectation')
        expect(p.label).toBe(want.label)
        expect(round(p.line)).toBe(want.line)
        expect(round(p.ahead)).toBe(want.ahead)
        expect(round(p.margin)).toBe(want.margin)
        expect(p.active).toBe(want.active)
        expect(round(p.delaySeconds)).toBe(want.delaySeconds)
        expect(p.active ? p.catchupAt - c.now : 0).toBe(want.catchup_in)
        expect(round(p.holdAt)).toBe(want.holdAt)
      })
    })
  }
})

describe('weekly profile', () => {
  const work = parseConfig({ pace_profile_days: [1, 1, 1, 1, 1, 0.3, 0.3], pace_profile_hours: [...Array(8).fill(0.1), ...Array(15).fill(1), 0.1] })
  const MONDAY = 950400 // 1970-01-12 00:00 UTC
  const WEEK = 7 * 86400

  test('a uniform profile is the even-spend line', () => {
    const cfg = parseConfig({})
    expect(paceLine(95, MONDAY + WEEK, WEEK, MONDAY + WEEK / 2, profileOf(cfg, 0))).toBe(47.5)
    expect(paceLine(95, MONDAY + WEEK, WEEK, MONDAY + WEEK / 2, profileOf(cfg, 120))).toBe(47.5)
  })

  test('the line stands still at night and climbs through the working day', () => {
    const p = profileOf(work, 0)
    const night = paceLine(90, MONDAY + WEEK, WEEK, MONDAY + 4 * 3600, p)
    const noon = paceLine(90, MONDAY + WEEK, WEEK, MONDAY + 12 * 3600, p)
    const evening = paceLine(90, MONDAY + WEEK, WEEK, MONDAY + 20 * 3600, p)
    expect(night).toBeLessThan(1)
    expect(noon - night).toBeGreaterThan(3)
    expect(evening - noon).toBeGreaterThan(7)
    expect(paceLine(90, MONDAY + WEEK, WEEK, MONDAY + WEEK, p)).toBe(90)
  })

  test('the local offset moves the profile', () => {
    // 09:00 UTC is a working hour at UTC+0 and 01:00 at UTC-8: the line climbs there at the night rate
    const at = MONDAY + 9 * 3600
    expect(lineRateAt(90, MONDAY + WEEK, WEEK, at, profileOf(work, -480))).toBeLessThan(lineRateAt(90, MONDAY + WEEK, WEEK, at, profileOf(work, 0)) / 5)
  })

  test('the catch-up time walks the profile', () => {
    const p = profileOf(work, 0)
    const usage = { ts: MONDAY + 12 * 3600, windows: [{ key: 'seven_day' as const, pct: 40, resetsAt: MONDAY + WEEK }] }
    const [pace] = paces(usage, work, MONDAY + 12 * 3600, terms('high', {}, work), p)
    if (pace === undefined) throw new Error('no pace')
    expect(pace.active).toBe(true)
    expect(Math.abs(paceLine(90, MONDAY + WEEK, WEEK, pace.catchupAt, p) - 25)).toBeLessThan(0.3)
    expect(pace.lineRate).toBeGreaterThan(0)
  })

  test('a profile of the wrong length is left at the default', () => {
    expect(parseConfig({ pace_profile_days: [1, 2] }).pace_profile_days.length).toBe(7)
    expect(parseConfig({ pace_profile_hours: Array(24).fill(-1) }).pace_profile_hours[0]).toBe(1)
  })
})

describe('the weekly hold level', () => {
  const cfg = parseConfig({})
  const week = WINDOWS[1]
  if (week === undefined) throw new Error('no week')

  test('stays at the threshold until the release window, then climbs to 100 at the reset', () => {
    const reset = 1_000_000
    expect(holdLevel(cfg, week, reset, reset - 3 * 86400)).toBe(90)
    expect(holdLevel(cfg, week, reset, reset - 24 * 3600)).toBe(90)
    expect(holdLevel(cfg, week, reset, reset - 12 * 3600)).toBe(95)
    expect(holdLevel(cfg, week, reset, reset - 6 * 3600)).toBe(97.5)
    expect(holdLevel(cfg, week, reset, reset)).toBe(100)
    expect(holdLevel(parseConfig({ threshold_7d_release_hours: 0 }), week, reset, reset - 60)).toBe(90)
  })

  test('the 5h window never releases, and violations use the released level', () => {
    const five = WINDOWS[0]
    if (five === undefined) throw new Error('no 5h')
    expect(holdLevel(cfg, five, 1000, 900)).toBe(95)
    const usage = { ts: 0, windows: [{ key: 'seven_day' as const, pct: 94, resetsAt: 1_000_000 }] }
    expect(violations(usage, cfg, 1_000_000 - 2 * 86400).map(v => v.label)).toEqual(['7d'])
    expect(violations(usage, cfg, 1_000_000 - 12 * 3600)).toEqual([])
    const [p] = paces(usage, cfg, 1_000_000 - 12 * 3600, terms('high', {}, cfg))
    expect(p?.holdAt).toBe(95)
    expect(p?.threshold).toBe(90)
  })
})

describe('config', () => {
  test('a key of the wrong type and an unknown key are left at the default', () => {
    const cfg = parseConfig({ threshold_5h: '80', pace_mode: 'sideways', nope: 1, poll_seconds: 2 })
    expect(cfg.threshold_5h).toBe(95)
    expect(cfg.pace_mode).toBe('hold')
    expect(cfg.poll_seconds).toBe(2)
  })

  test('a corrupt document is the defaults entirely', () => {
    expect(parseConfig(undefined).threshold_7d).toBe(90)
    expect(parseConfig('junk').enabled).toBe(true)
  })
})

describe('borrow ramp', () => {
  test('nothing before `after`, everything from `full`, linear between', () => {
    expect(ramp(0, 120, 600)).toBe(0)
    expect(ramp(120, 120, 600)).toBe(0)
    expect(ramp(360, 120, 600)).toBe(0.5)
    expect(ramp(600, 120, 600)).toBe(1)
    expect(ramp(99999, 120, 600)).toBe(1)
  })

  test('a class with no session at all is idle forever', () => {
    const cfg = parseConfig({})
    expect(terms('low', {}, cfg).lift).toBe(2)
    expect(terms('normal', { high: 0 }, cfg).lift).toBe(0)
  })

  test('a low session rises past normal only once normal is fully idle', () => {
    const cfg = parseConfig({})
    const half = terms('low', { normal: 360, high: 99999 }, cfg)
    expect(round(half.lift)).toBe(0.5)
    expect(round(half.marginFactor)).toBe(0.25)
    const whole = terms('low', { normal: 600, high: 360 }, cfg)
    expect(round(whole.lift)).toBe(1.5)
    expect(round(whole.marginFactor)).toBe(0.75)
  })

  test('priorities cycle low, normal, high, low', () => {
    expect(nextPriority('low')).toBe('normal')
    expect(nextPriority('normal')).toBe('high')
    expect(nextPriority('high')).toBe('low')
  })
})

describe('registry', () => {
  const entry = (id: string, priority: Priority, lastCall: number): SessionEntry => ({
    id, priority, cwd: '/w', started: 0, last_call: lastCall, updated: lastCall, hold: null, pacing_seconds: 0, lift: 0,
  })

  test('idleAbove is the most recent call per class, never this session', () => {
    const idle = idleAbove([entry('me', 'high', 1000), entry('a', 'high', 900), entry('b', 'high', 500), entry('c', 'normal', 100)], 'me', 1000)
    expect(idle).toEqual({ high: 100, normal: 900 })
  })

  test('a session file round-trips and a broken one is dropped', () => {
    const e = entry('s1', 'low', 10)
    expect(sessionFromFile(JSON.parse(JSON.stringify(e)))).toEqual(e)
    expect(sessionFromFile({ id: 's1', priority: 'urgent' })).toBe(undefined)
    expect(sessionFromFile('x')).toBe(undefined)
  })
})

describe('usage', () => {
  test('the engine reading becomes the cache shape and back', () => {
    const usage = usageFromSession([{ kind: 'five_hour', percentUsed: 12.5, resetsAt: '2026-10-10T12:00:00Z' }, { kind: 'spend_limit', percentUsed: 1 }], 1000)
    expect(usage).toEqual({ ts: 1000, windows: [{ key: 'five_hour', pct: 12.5, resetsAt: Date.parse('2026-10-10T12:00:00Z') / 1000 }] })
    if (usage === undefined) throw new Error('unreachable')
    expect(usageFromFile(usageToFile(usage))).toEqual(usage)
    expect(usageFromSession([], 1)).toBe(undefined)
    expect(usageFromFile({ ts: 1 })).toBe(undefined)
  })

  test('a merge carries a window the fresh reading lacks until it resets', () => {
    const previous = { ts: 100, windows: [{ key: 'seven_day' as const, pct: 50, resetsAt: 5000 }, { key: 'five_hour' as const, pct: 9, resetsAt: 150 }] }
    const fresh = { ts: 200, windows: [{ key: 'five_hour' as const, pct: 10, resetsAt: 9000 }] }
    expect(mergeUsage(fresh, previous, 200).windows).toEqual([{ key: 'five_hour', pct: 10, resetsAt: 9000 }, { key: 'seven_day', pct: 50, resetsAt: 5000 }])
    expect(mergeUsage(fresh, previous, 6000).windows).toEqual([{ key: 'five_hour', pct: 10, resetsAt: 9000 }])
  })

  test('violations are windows at or over the threshold that have not reset', () => {
    const cfg: Config = parseConfig({ threshold_5h: 50 })
    const usage = { ts: 0, windows: [{ key: 'five_hour' as const, pct: 50, resetsAt: 100 }, { key: 'seven_day' as const, pct: 95, resetsAt: 400_000 }] }
    expect(violations(usage, cfg, 50).map(v => v.label)).toEqual(['5h', '7d'])
    expect(violations(usage, cfg, 150).map(v => v.label)).toEqual(['7d'])
  })

  test('durations read as the statusline showed them', () => {
    expect(fmtDuration(30)).toBe('<1m')
    expect(fmtDuration(600)).toBe('10m')
    expect(fmtDuration(3600 * 5 + 120)).toBe('5h02m')
    expect(fmtDuration(86400 * 2 + 3600 * 3)).toBe('2d3h')
  })
})
