import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  GitPullRequest,
  Search,
  Zap,
} from 'lucide-react'
import { reviews, tokens } from '../api/client'
import type { ReviewRun } from '../types'

const STATUS_BADGE: Record<string, { cls: string; label: string }> = {
  completed: { cls: 'badge-success', label: '完成' },
  failed: { cls: 'badge-error', label: '失败' },
  running: { cls: 'badge-info', label: '运行中' },
}

function formatTokens(n: number): string {
  if (!n) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function Reviews() {
  const [runs, setRuns] = useState<ReviewRun[]>([])
  const [tokenMap, setTokenMap] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  useEffect(() => {
    reviews
      .list({ limit: 100 })
      .then(async (r) => {
        setRuns(r.runs)
        // Fetch token usage for each run
        const map: Record<string, number> = {}
        await Promise.all(
          r.runs.map(async (run) => {
            try {
              const t = await tokens.byRun(run.run_id)
              map[run.run_id] = t.total_tokens || 0
            } catch {
              map[run.run_id] = 0
            }
          })
        )
        setTokenMap(map)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  const filtered = runs.filter(
    (r) =>
      r.repo.toLowerCase().includes(search.toLowerCase()) ||
      String(r.pr_number).includes(search)
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">审查记录</h1>
          <p className="text-sm text-gray-500 mt-1">所有 PR 审查历史</p>
        </div>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="搜索仓库或 PR..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-10 pr-4 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-brand-500 w-64"
          />
        </div>
      </div>

      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-gray-400">
            加载中...
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  状态
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  仓库 / PR
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Commit
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  发现
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  确认
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                  Token
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                  时间
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filtered.map((run) => {
                const badge = STATUS_BADGE[run.status] ?? { cls: 'badge-gray', label: run.status }
                const tok = tokenMap[run.run_id]
                return (
                  <tr
                    key={run.run_id}
                    className="hover:bg-gray-50 transition-colors"
                  >
                    <td className="px-6 py-4">
                      <span className={`badge ${badge.cls}`}>{badge.label}</span>
                    </td>
                    <td className="px-6 py-4">
                      <Link
                        to={`/reviews/${run.run_id}`}
                        className="text-brand-600 hover:text-brand-700 font-medium"
                      >
                        {run.repo}
                      </Link>
                      <span className="text-gray-400 ml-1">#{run.pr_number}</span>
                    </td>
                    <td className="px-6 py-4 font-mono text-sm text-gray-500">
                      {run.head_sha?.slice(0, 8) || '—'}
                    </td>
                    <td className="px-6 py-4 text-right font-medium">
                      {run.summary?.total_findings ?? 0}
                    </td>
                    <td className="px-6 py-4 text-right text-green-600 font-medium">
                      {run.summary?.confirmed ?? 0}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-1 text-sm">
                        <Zap className="w-3.5 h-3.5 text-yellow-500" />
                        <span className={tok ? 'font-medium text-gray-700' : 'text-gray-400'}>
                          {formatTokens(tok ?? 0)}
                        </span>
                      </div>
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-500">
                      {new Date(run.started_at).toLocaleString('zh-CN')}
                    </td>
                  </tr>
                )
              })}
              {!filtered.length && (
                <tr>
                  <td colSpan={7} className="px-6 py-12 text-center text-gray-400">
                    暂无审查记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
