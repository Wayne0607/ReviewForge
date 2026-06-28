// ── API Response Types ──────────────────────────────────────

export interface ReviewRun {
  run_id: string
  repo: string
  pr_number: number
  head_sha: string
  base_sha: string
  status: 'running' | 'completed' | 'failed'
  started_at: string
  completed_at: string | null
  summary: ReviewSummary
}

export interface ReviewSummary {
  total_findings: number
  confirmed: number
  false_positives: number
  tasks_completed: number
  tasks_failed: number
}

export interface Finding {
  id: string
  run_id: string
  file: string
  line: number
  severity: 'error' | 'warning' | 'info'
  category: string
  message: string
  suggestion: string
  confidence: number
  reviewer: string
  status: 'candidate' | 'confirmed' | 'false_positive' | 'reported'
  verified_by: string
}

export interface ReviewerMetric {
  id: number
  run_id: string
  reviewer_name: string
  findings_count: number
  duration_ms: number
  status: 'completed' | 'failed'
  error: string
}

export interface SummaryStats {
  total_runs: number
  total_findings: number
  confirmed: number
  false_positives: number
  avg_confidence: number
}

export interface CategoryCount {
  category: string
  count: number
}

export interface WeeklyTrend {
  week: string
  total: number
  confirmed: number
}

export interface HotspotFile {
  file: string
  count: number
  confirmed: number
}

export interface ReviewerStats {
  reviewer_name: string
  total_runs: number
  total_findings: number
  avg_duration_ms: number
  success_count: number
}

export interface RecurringIssue {
  file: string
  category: string
  run_count: number
  total_count: number
}

export interface AgentSpec {
  role: string
  description: string
}

export interface SystemSpecs {
  agents: Record<string, AgentSpec>
  tools: Record<string, { description: string }>
  skills: string[]
}
