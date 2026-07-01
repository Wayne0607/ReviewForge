import { useEffect, useMemo, useState } from 'react'
import {
  GitPullRequest,
  Bug,
  CheckCircle2,
  BarChart3,
  TrendingUp,
  Zap,
} from 'lucide-react'
import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { metrics, reviews, tokens } from '../api/client'
import StatsCard from '../components/StatsCard'
import ReviewTimeline from '../components/ReviewTimeline'
import TrendChart from '../components/TrendChart'
import TokenUsageCard from '../components/TokenUsageCard'
import type { SummaryStats, CategoryCount, WeeklyTrend, ReviewRun } from '../types'

const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#14b8a6']

/** Group small categories into "其他" to prevent label overlap. */
function groupCategories(data: CategoryCount[], topN = 6): CategoryCount[] {
  if (data.length <= topN) return data
  const sorted = [...data].sort((a, b) => b.count - a.count)
  const top = sorted.slice(0, topN)
  const rest = sorted.slice(topN)
  const otherCount = rest.reduce((sum, c) => sum + c.count, 0)
  return [...top, { category: '其他', count: otherCount }]
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function Dashboard() {
  const [stats, setStats] = useState<SummaryStats | null>(null)
  const [categories, setCategories] = useState<CategoryCount[]>([])
  const [trends, setTrends] = useState<WeeklyTrend[]>([])
  const [recentRuns, setRecentRuns] = useState<ReviewRun[]>([])
  const [tokenSummary, setTokenSummary] = useState<{ total_tokens: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.allSettled([
      metrics.summary(),
      metrics.categories(),
      metrics.trends(),
      reviews.list({ limit: 5 }),
      tokens.summary(),
    ]).then((results) => {
      const [s, c, t, r, ts] = results
      if (s.status === 'fulfilled') setStats(s.value)
      else setError(s.reason?.message || '加载统计数据失败')
      if (c.status === 'fulfilled') setCategories(c.value)
      if (t.status === 'fulfilled') setTrends(t.value)
      if (r.status === 'fulfilled') setRecentRuns(r.value.runs)
      if (ts.status === 'fulfilled') setTokenSummary(ts.value)
    }).finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64" role="status" aria-live="polite">
        <div className="text-gray-400">加载中...</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <div className="text-red-500">⚠️ {error}</div>
        <button onClick={() => window.location.reload()} className="btn btn-primary">重试</button>
      </div>
    )
  }

  const confirmRate = stats?.total_findings
    ? Math.round(((stats.confirmed || 0) / stats.total_findings) * 100)
    : 0

  const grouped = useMemo(() => groupCategories(categories), [categories])

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">总览</h1>
        <p className="text-sm text-gray-500 mt-1">ReviewForge 多 Agent 代码审查系统</p>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatsCard
          title="审查总数"
          value={stats?.total_runs ?? 0}
          icon={<GitPullRequest className="w-6 h-6" />}
        />
        <StatsCard
          title="发现总数"
          value={stats?.total_findings ?? 0}
          icon={<Bug className="w-6 h-6" />}
        />
        <StatsCard
          title="确认率"
          value={`${confirmRate}%`}
          subtitle={`${stats?.confirmed ?? 0} confirmed`}
          icon={<CheckCircle2 className="w-6 h-6" />}
          trend={confirmRate > 70 ? 'up' : 'down'}
        />
        <StatsCard
          title="Token 消耗"
          value={formatTokens(tokenSummary?.total_tokens ?? 0)}
          icon={<Zap className="w-6 h-6" />}
        />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Category distribution */}
        <div className="card">
          <div className="card-header">问题分类分布</div>
          <div className="card-body">
            {categories.length > 0 ? (
              <div className="flex flex-col items-center">
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie
                      data={grouped}
                      dataKey="count"
                      nameKey="category"
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={80}
                      paddingAngle={2}
                    >
                      {grouped.map((_, i) => (
                        <Cell key={i} fill={COLORS[i % COLORS.length]} />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(value: number, name: string) => [`${value} 个`, name]}
                    />
                  </PieChart>
                </ResponsiveContainer>
                {/* Custom legend */}
                <div className="flex flex-wrap justify-center gap-x-4 gap-y-1 mt-2">
                  {grouped.map((item, i) => (
                    <div key={item.category} className="flex items-center gap-1.5 text-xs">
                      <span
                        className="w-2.5 h-2.5 rounded-full inline-block"
                        style={{ backgroundColor: COLORS[i % COLORS.length] }}
                      />
                      <span className="text-gray-600">{item.category}</span>
                      <span className="text-gray-400">({item.count})</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-center h-64 text-gray-400">
                暂无分类数据
              </div>
            )}
          </div>
        </div>

        {/* Trend chart */}
        <div className="card">
          <div className="card-header">发现趋势（按周）</div>
          <div className="card-body">
            <TrendChart data={trends} />
          </div>
        </div>
      </div>

      {/* Recent reviews */}
      <div className="card">
        <div className="card-header">最近审查</div>
        <div className="card-body">
          <ReviewTimeline runs={recentRuns} />
        </div>
      </div>
    </div>
  )
}
