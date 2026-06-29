import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  ArrowLeft,
  GitPullRequest,
  CheckCircle2,
  XCircle,
  Zap,
} from 'lucide-react'
import { reviews, tokens } from '../api/client'
import FindingBadge from '../components/FindingBadge'
import TokenUsageCard from '../components/TokenUsageCard'
import type { ReviewRun, Finding, ReviewerMetric } from '../types'

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function ReviewDetail() {
  const { runId } = useParams<{ runId: string }>()
  const [run, setRun] = useState<ReviewRun | null>(null)
  const [findings, setFindings] = useState<Finding[]>([])
  const [metrics, setMetrics] = useState<ReviewerMetric[]>([])
  const [tokenData, setTokenData] = useState<{ agents: { agent_name: string; total_tokens: number }[]; total_tokens: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<string>('all')

  useEffect(() => {
    if (!runId) return
    let cancelled = false
    setLoading(true)
    setError(null)

    Promise.all([
      reviews.detail(runId),
      tokens.byRun(runId).catch(() => null),
    ]).then(([r, t]) => {
      if (cancelled) return
      setRun(r.run)
      setFindings(r.findings)
      setMetrics(r.metrics)
      setTokenData(t)
    }).catch((e) => {
      if (!cancelled) setError(e.message)
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })

    return () => { cancelled = true }
  }, [runId])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400" role="status" aria-live="polite">
        加载中...
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <div className="text-red-500">⚠️ {error}</div>
        <Link to="/reviews" className="btn btn-primary">返回列表</Link>
      </div>
    )
  }

  if (!run) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-500">审查记录未找到</p>
        <Link to="/reviews" className="text-brand-600 hover:underline mt-2 inline-block">
          返回列表
        </Link>
      </div>
    )
  }

  const filtered =
    filter === 'all'
      ? findings
      : findings.filter((f) => f.status === filter)

  const statusCounts = {
    all: findings.length,
    confirmed: findings.filter((f) => f.status === 'confirmed').length,
    false_positive: findings.filter((f) => f.status === 'false_positive').length,
    candidate: findings.filter((f) => f.status === 'candidate').length,
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Link
          to="/reviews"
          className="p-2 rounded-lg hover:bg-gray-200 transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
        </Link>
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            <GitPullRequest className="w-6 h-6" />
            {run.repo} #{run.pr_number}
          </h1>
          <p className="text-sm text-gray-500 mt-1 font-mono">
            {run.head_sha?.slice(0, 12) || '—'} ·{' '}
            {new Date(run.started_at).toLocaleString('zh-CN')}
          </p>
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <div className="card card-body text-center">
          <div className="text-2xl font-bold text-gray-900">
            {run.summary?.total_findings ?? 0}
          </div>
          <div className="text-sm text-gray-500">总发现</div>
        </div>
        <div className="card card-body text-center">
          <div className="text-2xl font-bold text-green-600">
            {run.summary?.confirmed ?? 0}
          </div>
          <div className="text-sm text-gray-500">已确认</div>
        </div>
        <div className="card card-body text-center">
          <div className="text-2xl font-bold text-red-500">
            {run.summary?.false_positives ?? 0}
          </div>
          <div className="text-sm text-gray-500">误报</div>
        </div>
        <div className="card card-body text-center">
          <div className="text-2xl font-bold text-gray-700">
            {metrics.filter(m => m.status === 'completed').length} / {metrics.length}
          </div>
          <div className="text-sm text-gray-500">Reviewer 完成</div>
        </div>
      </div>

      {/* Token usage */}
      {tokenData && tokenData.total_tokens > 0 && (
        <TokenUsageCard
          totalTokens={tokenData.total_tokens}
          agents={tokenData.agents}
        />
      )}

      {/* Reviewer metrics */}
      {metrics.length > 0 && (
        <div className="card">
          <div className="card-header">Reviewer 执行详情</div>
          <div className="card-body">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {metrics.map((m) => (
                <div
                  key={m.id}
                  className="flex items-center gap-3 p-3 rounded-lg bg-gray-50"
                >
                  {m.status === 'completed' ? (
                    <CheckCircle2 className="w-5 h-5 text-green-500 shrink-0" />
                  ) : (
                    <XCircle className="w-5 h-5 text-red-500 shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">
                      {m.reviewer_name}
                    </div>
                    <div className="text-xs text-gray-500">
                      {m.findings_count} 发现 · {(m.duration_ms / 1000).toFixed(1)}s
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Findings filter */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <span>发现列表</span>
          <div className="flex gap-2">
            {(['all', 'confirmed', 'false_positive', 'candidate'] as const).map((s) => (
              <button
                key={s}
                onClick={() => setFilter(s)}
                className={`px-3 py-1 text-xs rounded-full transition-colors ${
                  filter === s
                    ? 'bg-brand-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                }`}
              >
                {s === 'all' ? '全部' : s === 'confirmed' ? '已确认' : s === 'false_positive' ? '误报' : '待验证'}
                <span className="ml-1">({statusCounts[s]})</span>
              </button>
            ))}
          </div>
        </div>
        <div className="card-body space-y-3">
          {filtered.length > 0 ? (
            filtered.map((f) => <FindingBadge key={f.id} finding={f} />)
          ) : (
            <div className="text-center py-8 text-gray-400">
              {filter === 'all' ? '暂无发现' : `暂无${filter === 'confirmed' ? '已确认' : filter === 'false_positive' ? '误报' : '待验证'}的发现`}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
