// Pace math: pure functions over plain data, no `$`. Mirrored in guardlib.py
// for the CLI and the Codex hooks; hooks/pace-cases.ts pins both to the same
// answers.
import type { SessionRateLimit } from 'claude-code'

import { DEFAULTS } from './defaults'

export type Config = {
  enabled: boolean
  threshold_5h: number
  threshold_7d: number
  threshold_7d_release_hours: number
  poll_seconds: number
  stale_after_seconds: number
  pace_enabled: boolean
  pace_mode: 'delay' | 'hold'
  pace_margin_5h: number
  pace_margin_7d: number
  pace_min_used_pct: number
  pace_seconds_per_pct: number
  pace_max_delay_seconds: number
  pace_profile_days: number[]
  pace_profile_hours: number[]
  priority_margin_factor_normal: number
  priority_margin_factor_low: number
  priority_delay_factor_normal: number
  priority_delay_factor_low: number
  borrow_after_seconds: number
  borrow_full_seconds: number
  session_stale_seconds: number
  codex_log_max_age_seconds: number
}

export type Priority = 'low' | 'normal' | 'high'
export const PRIORITIES: readonly Priority[] = ['low', 'normal', 'high']

export const isPriority = (value: unknown): value is Priority =>
  typeof value === 'string' && (PRIORITIES as readonly string[]).includes(value)

export const nextPriority = (priority: Priority): Priority =>
  PRIORITIES[(PRIORITIES.indexOf(priority) + 1) % PRIORITIES.length] ?? 'normal'

export type WindowKey = 'five_hour' | 'seven_day'

export type Window = {
  key: WindowKey
  label: '5h' | '7d'
  seconds: number
  thresholdKey: 'threshold_5h' | 'threshold_7d'
  marginKey: 'pace_margin_5h' | 'pace_margin_7d'
}

export const WINDOWS: readonly Window[] = [
  { key: 'five_hour', label: '5h', seconds: 5 * 3600, thresholdKey: 'threshold_5h', marginKey: 'pace_margin_5h' },
  { key: 'seven_day', label: '7d', seconds: 7 * 86400, thresholdKey: 'threshold_7d', marginKey: 'pace_margin_7d' },
]

/** One window's reading: percent used and when it resets, in epoch seconds. */
export type Reading = { key: WindowKey; pct: number; resetsAt: number }

/** A set of readings and when they were taken, in epoch seconds. */
export type Usage = { ts: number; windows: Reading[] }

/** The DEFAULTS table typed; the JSON literal is checked against Config here. */
export const CONFIG_DEFAULTS: Config = DEFAULTS as Config

/**
 * `stored` (a parsed config.json) over the defaults. A key the table does not
 * have, or a value of another type than its default, is left at the default:
 * the file is hand-edited and a typo must never break a tool call.
 */
export function parseConfig(stored: unknown): Config {
  const cfg: Record<string, unknown> = { ...CONFIG_DEFAULTS }
  if (typeof stored === 'object' && stored !== null) {
    for (const [key, value] of Object.entries(stored)) {
      if (!(key in cfg)) continue
      const current = cfg[key]
      if (Array.isArray(current)) {
        if (Array.isArray(value) && value.length === current.length && value.every(v => typeof v === 'number' && v >= 0)) cfg[key] = value
      } else if (typeof value === typeof current) cfg[key] = value
    }
  }
  if (cfg['pace_mode'] !== 'delay' && cfg['pace_mode'] !== 'hold') cfg['pace_mode'] = 'hold'
  return cfg as Config
}

export const isStale = (usage: Usage | undefined, cfg: Config, now: number): usage is undefined =>
  usage === undefined || now - usage.ts > cfg.stale_after_seconds

/**
 * A weekly spending profile: a weight per day of the week (Monday first) and
 * per hour of the day, in local time; `offsetMinutes` is the local offset from
 * UTC (what `getTimezoneOffset()` answers, negated). Uniform weights give the
 * even-spend line; a work-week profile lets the line climb during working
 * hours and stand still at night and on weekends.
 */
export type Profile = { days: readonly number[]; hours: readonly number[]; offsetMinutes: number }

export const UNIFORM: Profile = { days: [1, 1, 1, 1, 1, 1, 1], hours: Array(24).fill(1), offsetMinutes: 0 }

export const profileOf = (cfg: Config, offsetMinutes: number): Profile => ({ days: cfg.pace_profile_days, hours: cfg.pace_profile_hours, offsetMinutes })

export const isUniform = (profile: Profile): boolean =>
  profile.days.every(w => w === profile.days[0]) && profile.hours.every(w => w === profile.hours[0])

/** The profile's weight at epoch second `t`. */
export function weightAt(profile: Profile, t: number): number {
  const local = t + profile.offsetMinutes * 60
  const dayIndex = (Math.floor(local / 86400) + 3) % 7 // 1970-01-01 was a Thursday; Monday is 0
  const hour = Math.floor((local % 86400) / 3600)
  return (profile.days[dayIndex] ?? 1) * (profile.hours[hour] ?? 1)
}

/** ∫ weight over [from, to], hour by hour. */
export function weightBetween(profile: Profile, from: number, to: number): number {
  if (to <= from) return 0
  let total = 0
  let t = from
  while (t < to) {
    const local = t + profile.offsetMinutes * 60
    const nextHour = t + (3600 - (local % 3600))
    const end = Math.min(nextHour, to)
    total += weightAt(profile, t) * (end - t)
    t = end
  }
  return total
}

/**
 * Usage the pace line allows at `now`: the threshold scaled by the share of the
 * window's spending profile that has elapsed (the elapsed fraction under a
 * uniform profile).
 */
export function paceLine(threshold: number, resetsAt: number, windowSeconds: number, now: number, profile: Profile = UNIFORM): number {
  const start = resetsAt - windowSeconds
  const at = Math.min(Math.max(now, start), resetsAt)
  if (isUniform(profile)) return threshold * ((at - start) / windowSeconds)
  const whole = weightBetween(profile, start, resetsAt)
  if (whole <= 0) return threshold * ((at - start) / windowSeconds)
  return threshold * (weightBetween(profile, start, at) / whole)
}

/** Points per second the line climbs at `now`. */
export function lineRateAt(threshold: number, resetsAt: number, windowSeconds: number, now: number, profile: Profile = UNIFORM): number {
  if (isUniform(profile)) return threshold / windowSeconds
  const whole = weightBetween(profile, resetsAt - windowSeconds, resetsAt)
  return whole <= 0 ? threshold / windowSeconds : (threshold * weightAt(profile, now)) / whole
}

/** The first time at or after `now` when the line reaches `level`, at most `resetsAt`. */
export function lineReaches(threshold: number, resetsAt: number, windowSeconds: number, now: number, level: number, profile: Profile = UNIFORM): number {
  if (threshold <= 0 || level >= threshold) return resetsAt
  if (isUniform(profile)) {
    const t = Math.floor(resetsAt - windowSeconds * (1 - level / threshold))
    return Math.max(Math.floor(now), Math.min(t, resetsAt))
  }
  let t = Math.max(now, resetsAt - windowSeconds)
  while (t < resetsAt) {
    const local = t + profile.offsetMinutes * 60
    const end = Math.min(t + (3600 - (local % 3600)), resetsAt)
    const lineEnd = paceLine(threshold, resetsAt, windowSeconds, end, profile)
    if (lineEnd >= level) {
      const lineStart = paceLine(threshold, resetsAt, windowSeconds, t, profile)
      const f = lineEnd > lineStart ? (level - lineStart) / (lineEnd - lineStart) : 1
      return Math.max(Math.floor(now), Math.floor(t + f * (end - t)))
    }
    t = end
  }
  return resetsAt
}

export type Violation = { label: string; pct: number; resetsAt: number; threshold: number }

/**
 * The level the hold engages at right now. The weekly threshold keeps a
 * reserve all week and releases it over the last `threshold_7d_release_hours`,
 * climbing linearly to 100% at the reset: on the last day there is nothing
 * left to save it for. The pace line still aims at the base threshold.
 */
export function holdLevel(cfg: Config, w: Window, resetsAt: number, now: number): number {
  const base = cfg[w.thresholdKey]
  if (w.key !== 'seven_day' || cfg.threshold_7d_release_hours <= 0) return base
  const release = cfg.threshold_7d_release_hours * 3600
  const remaining = Math.max(0, resetsAt - now)
  if (remaining >= release) return base
  return Math.min(100, base + (100 - base) * (1 - remaining / release))
}

/** Windows at or over their hold level whose reset is still ahead. */
export function violations(usage: Usage, cfg: Config, now: number): Violation[] {
  const found: Violation[] = []
  for (const w of WINDOWS) {
    const r = usage.windows.find(one => one.key === w.key)
    if (r === undefined) continue
    const threshold = holdLevel(cfg, w, r.resetsAt, now)
    if (r.pct >= threshold && r.resetsAt > now) {
      found.push({ label: w.label, pct: r.pct, resetsAt: r.resetsAt, threshold })
    }
  }
  return found
}

/**
 * A session's terms: what fraction of the window margin it gets and how much
 * its delays are stretched. `lift` is how far it has risen above its own class
 * by borrowing from idle ones (0 = its own terms, 1 = fully the next class's).
 */
export type Terms = { marginFactor: number; delayFactor: number; lift: number }

const ownTerms = (priority: Priority, cfg: Config): { marginFactor: number; delayFactor: number } => {
  switch (priority) {
    case 'high':
      return { marginFactor: 1, delayFactor: 1 }
    case 'normal':
      return { marginFactor: cfg.priority_margin_factor_normal, delayFactor: cfg.priority_delay_factor_normal }
    case 'low':
      return { marginFactor: cfg.priority_margin_factor_low, delayFactor: cfg.priority_delay_factor_low }
  }
}

/** 0 before `after` seconds idle, 1 from `full`, linear between. */
export function ramp(idleSeconds: number, after: number, full: number): number {
  if (full <= after) return idleSeconds >= after ? 1 : 0
  return Math.min(1, Math.max(0, (idleSeconds - after) / (full - after)))
}

/**
 * The terms a session of `priority` runs under right now. `idleAbove[class]` is
 * how long ago any *other* session of that class last made a tool call; a
 * class with no session at all is absent and counts as idle forever. A session
 * borrows the next class's terms progressively as that class sits idle, and
 * only once it has borrowed them whole does it start on the class above that.
 */
export function terms(priority: Priority, idleAbove: Partial<Record<Priority, number>>, cfg: Config): Terms {
  const own = ownTerms(priority, cfg)
  let { marginFactor, delayFactor } = own
  let lift = 0
  for (let rank = PRIORITIES.indexOf(priority) + 1; rank < PRIORITIES.length; rank++) {
    const above = PRIORITIES[rank]
    if (above === undefined) break
    const idle = idleAbove[above] ?? Number.POSITIVE_INFINITY
    const f = ramp(idle, cfg.borrow_after_seconds, cfg.borrow_full_seconds)
    const target = ownTerms(above, cfg)
    marginFactor += f * (target.marginFactor - marginFactor)
    delayFactor += f * (target.delayFactor - delayFactor)
    lift += f
    if (f < 1) break
  }
  return { marginFactor, delayFactor, lift }
}

/** Where one window stands against its pace line under some terms. */
export type Pace = {
  key: WindowKey
  label: '5h' | '7d'
  pct: number
  resetsAt: number
  threshold: number
  line: number
  ahead: number
  margin: number
  active: boolean
  delaySeconds: number
  catchupAt: number
  lineRate: number   // points per second the line climbs right now
  holdAt: number     // the level the hold engages at right now (the threshold, released late in the week)
}

export const ACCOUNT_TERMS: Terms = { marginFactor: 1, delayFactor: 1, lift: 0 }

/**
 * One Pace per window in `usage`, whether or not pacing is engaged. The weekly
 * window follows `profile` (the 5h window is always even: it is short).
 */
export function paces(usage: Usage, cfg: Config, now: number, t: Terms, profile: Profile = UNIFORM): Pace[] {
  const out: Pace[] = []
  for (const w of WINDOWS) {
    const r = usage.windows.find(one => one.key === w.key)
    if (r === undefined) continue
    const threshold = cfg[w.thresholdKey]
    const margin = cfg[w.marginKey] * t.marginFactor
    const shape = w.key === 'seven_day' ? profile : UNIFORM
    const line = paceLine(threshold, r.resetsAt, w.seconds, now, shape)
    const ahead = r.pct - line
    const over = ahead - margin
    const active = cfg.pace_enabled && r.resetsAt > now && r.pct >= cfg.pace_min_used_pct && over > 0
    const delaySeconds = active
      ? Math.min(cfg.pace_max_delay_seconds * t.delayFactor, over * cfg.pace_seconds_per_pct * t.delayFactor)
      : 0
    const catchupAt = lineReaches(threshold, r.resetsAt, w.seconds, now, r.pct - margin, shape)
    const lineRate = lineRateAt(threshold, r.resetsAt, w.seconds, now, shape)
    out.push({ key: w.key, label: w.label, pct: r.pct, resetsAt: r.resetsAt, threshold, line, ahead, margin, active, delaySeconds, catchupAt, lineRate, holdAt: holdLevel(cfg, w, r.resetsAt, now) })
  }
  return out
}

/** The per-call delay pacing asks for: the worst window wins. */
export const paceDelay = (list: readonly Pace[]): number =>
  Math.max(0, ...list.filter(p => p.active).map(p => p.delaySeconds))

export function fmtDuration(seconds: number): string {
  const s = Math.floor(Math.max(0, seconds))
  if (s < 60) return '<1m'
  const minutes = Math.floor(s / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const m = minutes % 60
  if (hours < 24) return `${hours}h${String(m).padStart(2, '0')}m`
  const days = Math.floor(hours / 24)
  return `${days}d${hours % 24}h`
}

export const fmtClock = (epochSeconds: number): string => {
  const d = new Date(epochSeconds * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// --- the shapes on disk and the engine's, as plain data ---

export const num = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) ? value : undefined

/** usage.json's shape on disk, shared with the Python side. */
export type UsageFile = { ts: number } & Partial<Record<Reading['key'], { used_percentage: number; resets_at: number }>>

export function usageFromFile(doc: unknown): Usage | undefined {
  if (typeof doc !== 'object' || doc === null) return undefined
  const record = doc as Record<string, unknown>
  const ts = num(record['ts'])
  if (ts === undefined) return undefined
  const windows: Reading[] = []
  for (const w of WINDOWS) {
    const entry = record[w.key]
    if (typeof entry !== 'object' || entry === null) continue
    const pct = num((entry as Record<string, unknown>)['used_percentage'])
    const resetsAt = num((entry as Record<string, unknown>)['resets_at'])
    if (pct !== undefined && resetsAt !== undefined) windows.push({ key: w.key, pct, resetsAt })
  }
  return windows.length > 0 ? { ts, windows } : undefined
}

export function usageToFile(usage: Usage): UsageFile {
  const doc: UsageFile = { ts: usage.ts }
  for (const r of usage.windows) doc[r.key] = { used_percentage: r.pct, resets_at: r.resetsAt }
  return doc
}

/** The engine's rate limits as a Usage; undefined when no window has a reading. */
export function usageFromSession(limits: readonly SessionRateLimit[], now: number): Usage | undefined {
  const windows: Reading[] = []
  for (const w of WINDOWS) {
    const limit = limits.find(one => one.kind === w.key)
    if (limit === undefined || limit.resetsAt === undefined) continue
    const resetsAt = Date.parse(limit.resetsAt) / 1000
    if (!Number.isFinite(resetsAt)) continue
    windows.push({ key: w.key, pct: limit.percentUsed, resetsAt })
  }
  return windows.length > 0 ? { ts: now, windows } : undefined
}

/**
 * `fresh` over `previous`: a window `fresh` lacks is carried from `previous`
 * while its own reset is still ahead. The engine rebuilds the list per response
 * and a window can drop out of it transiently; forgetting it would blind the
 * guard to a window that still limits.
 */
export function mergeUsage(fresh: Usage, previous: Usage | undefined, now: number): Usage {
  const windows = [...fresh.windows]
  for (const r of previous?.windows ?? []) {
    if (r.resetsAt > now && !windows.some(one => one.key === r.key)) windows.push(r)
  }
  return { ts: fresh.ts, windows }
}

export const fresher = (a: Usage | undefined, b: Usage | undefined): Usage | undefined =>
  a === undefined ? b : b === undefined ? a : b.ts > a.ts ? b : a

export type Hold = { label: string; until: number; kind: 'threshold' | 'pace' }

export type SessionEntry = {
  id: string
  priority: Priority
  cwd: string
  started: number
  last_call: number
  updated: number
  hold: Hold | null
  pacing_seconds: number
  lift: number
}

export function sessionFromFile(doc: unknown): SessionEntry | undefined {
  if (typeof doc !== 'object' || doc === null) return undefined
  const r = doc as Record<string, unknown>
  const id = r['id']
  const priority = r['priority']
  const started = num(r['started'])
  const lastCall = num(r['last_call'])
  const updated = num(r['updated'])
  if (typeof id !== 'string' || !isPriority(priority) || started === undefined || lastCall === undefined || updated === undefined) {
    return undefined
  }
  const hold = r['hold']
  let parsedHold: Hold | null = null
  if (typeof hold === 'object' && hold !== null) {
    const h = hold as Record<string, unknown>
    const until = num(h['until'])
    if (typeof h['label'] === 'string' && until !== undefined && (h['kind'] === 'threshold' || h['kind'] === 'pace')) {
      parsedHold = { label: h['label'], until, kind: h['kind'] }
    }
  }
  return {
    id,
    priority,
    cwd: typeof r['cwd'] === 'string' ? r['cwd'] : '',
    started,
    last_call: lastCall,
    updated,
    hold: parsedHold,
    pacing_seconds: num(r['pacing_seconds']) ?? 0,
    lift: num(r['lift']) ?? 0,
  }
}

/** Seconds since another session of each class last made a tool call; absent when none exists. */
export function idleAbove(entries: readonly SessionEntry[], selfId: string, now: number): Partial<Record<Priority, number>> {
  const out: Partial<Record<Priority, number>> = {}
  for (const e of entries) {
    if (e.id === selfId) continue
    const idle = Math.max(0, now - e.last_call)
    const seen = out[e.priority]
    if (seen === undefined || idle < seen) out[e.priority] = idle
  }
  return out
}

