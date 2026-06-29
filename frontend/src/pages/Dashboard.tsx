import { useEffect, useState } from 'react'
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
  Legend,
} from 'recharts'
import { metrics, reviews, tokens } from '../api/client'
import StatsCard from '../components/StatsCard'
import ReviewTimeline from '../components/ReviewTimeline'
import TrendChart from '../components/TrendChart'
import TokenUsageCard from '../components/TokenUsageCard'
import type { SummaryStats, CategoryCount, WeeklyTrend, ReviewRun } from '../types'

const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4']

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

  useEffect(() => {
    Promise.all([
      metrics.summary(),
      metrics.categories(),
      metrics.trends(),
      reviews.list({ limit: 5 }),
      tokens.summary().catch(() => ({ total_tokens: 0 })),
    ]).then(([s, c, t, r, ts]) => {
      setStats(s)
      setCategories(c)
      setTrends(t)
      setRecentRuns(r.runs)
      setTokenSummary(ts)
    }).catch(console.error).finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-400">加载中...</div>
      </div>
    )
  }

  const confirmRate = stats?.total_findings
    ? Math.round(((stats.confirmed || 0) / stats.total_findings) * 100)
    : 0

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
              <ResponsiveContainer width="100%" height={250}>
                <PieChart>
                  <Pie
                    data={categories}
                    dataKey="count"
                    nameKey="category"
                    cx="50%"
                    cy="50%"
                    outerRadius={80}
                    label={({ category, percent }) =>
                      `${category} ${(percent * 100).toFixed(0)}%`
                    }
                    labelLine={false}
                    fontSize={11}
                  >
                    {categories.map((_, i) => (
                      <Cell key={i} fill={COLORS[i % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
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
