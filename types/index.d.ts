export type UsageGuardPriority = 'low' | 'normal' | 'high'

export type UsageGuardWindow = {
  label: string
  pct: number
  threshold: number
  ahead: number
  pacing: boolean
  delaySeconds: number
  resetsIn: number
}

export type UsageGuardView = {
  windows: UsageGuardWindow[]
  codex: { label: string; pct: number }[]
  hold: { label: string; until: number; kind: 'threshold' | 'pace' } | null
  pacing: { delaySeconds: number; backIn: number } | null
  lift: number
  stale: boolean
  ageSeconds: number
  enabled: boolean
  paceEnabled: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'usage-guard': { priority: UsageGuardPriority; view: UsageGuardView | null }
  }
}
