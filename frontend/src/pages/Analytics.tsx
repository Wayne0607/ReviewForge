import { useEffect, useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { metrics } from '../api/client'
import TrendChart from '../components/TrendChart'
import type { HotspotFile, ReviewerStats, RecurringIssue, WeeklyTrend } from '../types'

export default function Analytics() {
  const [hotspots, setHotspots] = useState<HotspotFile[]>([])
  const [reviewerStats, setReviewerStats] = useState<ReviewerStats[]>([])
  const [recurring, setRecurring] = useState<RecurringIssue[]>([])
  const [trends, setTrends] = useState<WeeklyTrend[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      metrics.hotspots(),
      metrics.reviewers(),
      metrics.recurring(),
      metrics.trends(),
    ])
      .then(([h, r, rec, t]) => {
        setHotspots(h)
        setReviewerStats(r)
        setRecurring(rec)
        setTrends(t)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        加载中...
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">趋势分析</h1>
        <p className="text-sm text-gray-500 mt-1">跨 PR 的问题趋势和热点分析</p>
      </div>

      {/* Trend chart */}
      <div className="card">
        <div className="card-header">发现趋势（按周）</div>
        <div className="card-body">
          <TrendChart data={trends} />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Hotspot files */}
        <div className="card">
          <div className="card-header">热点文件（问题最多的文件）</div>
          <div className="card-body">
            {hotspots.length > 0 ? (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart
                  data={hotspots}
                  layout="vertical"
                  margin={{ top: 5, right: 20, left: 100, bottom: 5 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis type="number" tick={{ fontSize: 12 }} />
                  <YAxis
                    type="category"
                    dataKey="file"
                    tick={{ fontSize: 11 }}
                    width={100}
                  />
                  <Tooltip />
                  <Bar dataKey="count" name="总发现" fill="#3b82f6" radius={[0, 4, 4, 0]} />
                  <Bar dataKey="confirmed" name="已确认" fill="#10b981" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex items-center justify-center h-64 text-gray-400">
                暂无热点数据
              </div>
            )}
          </div>
        </div>

        {/* Reviewer stats */}
        <div className="card">
          <div className="card-header">Reviewer 效率对比</div>
          <div className="card-body">
            {reviewerStats.length > 0 ? (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={reviewerStats}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="reviewer_name" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 12 }} />
                  <Tooltip />
                  <Bar
                    dataKey="total_findings"
                    name="总发现"
                    fill="#3b82f6"
                    radius={[4, 4, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex items-center justify-center h-64 text-gray-400">
                暂无 Reviewer 数据
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Reviewer details table */}
      {reviewerStats.length > 0 && (
        <div className="card">
          <div className="card-header">Reviewer 执行统计</div>
          <div className="card-body overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left py-2 px-3 font-medium text-gray-500">Reviewer</th>
                  <th className="text-right py-2 px-3 font-medium text-gray-500">执行次数</th>
                  <th className="text-right py-2 px-3 font-medium text-gray-500">总发现</th>
                  <th className="text-right py-2 px-3 font-medium text-gray-500">平均耗时</th>
                  <th className="text-right py-2 px-3 font-medium text-gray-500">成功率</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {reviewerStats.map((r) => (
                  <tr key={r.reviewer_name} className="hover:bg-gray-50">
                    <td className="py-2 px-3 font-medium">{r.reviewer_name}</td>
                    <td className="py-2 px-3 text-right">{r.total_runs}</td>
                    <td className="py-2 px-3 text-right font-medium">{r.total_findings}</td>
                    <td className="py-2 px-3 text-right">
                      {(r.avg_duration_ms / 1000).toFixed(1)}s
                    </td>
                    <td className="py-2 px-3 text-right">
                      {r.total_runs > 0
                        ? Math.round((r.success_count / r.total_runs) * 100)
                        : 0}
                      %
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Recurring issues */}
      <div className="card">
        <div className="card-header">反复出现的问题</div>
        <div className="card-body">
          {recurring.length > 0 ? (
            <div className="space-y-2">
              {recurring.map((r, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between p-3 bg-gray-50 rounded-lg"
                >
                  <div>
                    <span className="font-mono text-sm text-gray-700">{r.file}</span>
                    <span className="mx-2 text-gray-300">·</span>
                    <span className="badge badge-gray">{r.category}</span>
                  </div>
                  <div className="text-sm text-gray-500">
                    出现在 {r.run_count} 次审查中，共 {r.total_count} 个发现
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-8 text-gray-400">
              暂无反复出现的问题
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
