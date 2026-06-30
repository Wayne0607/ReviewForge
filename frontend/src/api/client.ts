// API client — typed fetch wrapper for ReviewForge dashboard.

const BASE = '/api/v1'

// Token resolution order: a token saved in the browser (Settings field in the sidebar)
// wins, so we never have to bake the secret into the public JS bundle; the build-time
// VITE_API_TOKEN is a fallback for local dev.
export const TOKEN_KEY = 'rf_api_token'

function getToken(): string {
  if (typeof localStorage !== 'undefined') {
    const t = localStorage.getItem(TOKEN_KEY)
    if (t) return t
  }
  return import.meta.env.VITE_API_TOKEN ?? ''
}

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: getHeaders() })
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`)
  return res.json()
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { ...getHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`)
  return res.json()
}

// ── Reviews ──────────────────────────────────────────────────

export const reviews = {
  list: (params?: { repo?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams()
    if (params?.repo) q.set('repo', params.repo)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return get<{ runs: import('../types').ReviewRun[] }>(`/dashboard/reviews${qs ? '?' + qs : ''}`)
  },
  detail: (runId: string) =>
    get<{
      run: import('../types').ReviewRun
      findings: import('../types').Finding[]
      metrics: import('../types').ReviewerMetric[]
    }>(`/dashboard/reviews/${runId}`),
}

// ── Metrics ──────────────────────────────────────────────────

export const metrics = {
  summary: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<import('../types').SummaryStats>(`/dashboard/metrics/summary${q}`)
  },
  categories: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<import('../types').CategoryCount[]>(`/dashboard/metrics/categories${q}`)
  },
  trends: (repo?: string, weeks?: number) => {
    const q = new URLSearchParams()
    if (repo) q.set('repo', repo)
    if (weeks) q.set('weeks', String(weeks))
    const qs = q.toString()
    return get<import('../types').WeeklyTrend[]>(`/dashboard/metrics/trends${qs ? '?' + qs : ''}`)
  },
  hotspots: (repo?: string, limit?: number) => {
    const q = new URLSearchParams()
    if (repo) q.set('repo', repo)
    if (limit) q.set('limit', String(limit))
    const qs = q.toString()
    return get<import('../types').HotspotFile[]>(`/dashboard/metrics/hotspots${qs ? '?' + qs : ''}`)
  },
  reviewers: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<import('../types').ReviewerStats[]>(`/dashboard/metrics/reviewers${q}`)
  },
  recurring: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<import('../types').RecurringIssue[]>(`/dashboard/metrics/recurring${q}`)
  },
}

// ── Token Usage ──────────────────────────────────────────────

export const tokens = {
  summary: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<{ total_prompt: number; total_completion: number; total_tokens: number; run_count: number }>(`/dashboard/tokens/summary${q}`)
  },
  byAgent: (repo?: string) => {
    const q = repo ? `?repo=${encodeURIComponent(repo)}` : ''
    return get<{ agent_name: string; total_tokens: number; call_count: number; avg_tokens: number }[]>(`/dashboard/tokens/by-agent${q}`)
  },
  byRun: (runId: string) =>
    get<{ run_id: string; agents: { agent_name: string; total_tokens: number; prompt_tokens: number; completion_tokens: number }[]; total_tokens: number }>(`/dashboard/tokens/${runId}`),
}

// ── System ───────────────────────────────────────────────────

export const system = {
  specs: () => get<import('../types').SystemSpecs>('/specs'),
  config: () => get<Record<string, unknown>>('/config'),
}

// ── Admin (console-driven Skill / config-type Agent CRUD) ────

export interface SkillMeta {
  name: string
  description: string
  category: string
  reviewer_type: string
  references: string[]
  is_builtin: boolean
}

export interface CustomAgent {
  reviewer_type: string
  name: string
  description: string
  allowed_tools: string[]
  model_profile: string
  max_steps: number
  instructions: string
  enabled: boolean
}

export interface BuiltinAgent {
  name: string
  role: string
  description: string
}

export const admin = {
  listSkills: () => get<{ skills: SkillMeta[] }>('/admin/skills'),
  getSkill: (name: string) =>
    get<{ name: string; raw: string; body: string; meta: { description: string; reviewer_type: string; category: string }; is_builtin: boolean }>(
      `/admin/skills/${name}`
    ),
  saveSkill: (s: { name: string; description: string; reviewer_type?: string; category?: string; body: string }) =>
    post<{ ok: boolean; skills_loaded: number }>('/admin/skills', s),
  deleteSkill: (name: string) => post<{ ok: boolean }>(`/admin/skills/${name}/delete`, {}),
  listAgents: () => get<{ custom: CustomAgent[]; builtin: BuiltinAgent[]; available_tools: string[] }>('/admin/agents'),
  saveAgent: (a: {
    reviewer_type: string
    description: string
    allowed_tools?: string[]
    model_profile?: string
    max_steps?: number
    instructions?: string
    enabled?: boolean
  }) => post<{ ok: boolean }>('/admin/agents', a),
  deleteAgent: (reviewerType: string) => post<{ ok: boolean }>(`/admin/agents/${reviewerType}/delete`, {}),
}
