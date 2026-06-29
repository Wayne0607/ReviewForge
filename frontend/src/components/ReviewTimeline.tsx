import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { GitPullRequest, CheckCircle2, XCircle, Loader2, Zap } from 'lucide-react'
import { tokens } from '../api/client'
import type { ReviewRun } from '../types'

const STATUS_ICON = {
  completed: <CheckCircle2 className="w-4 h-4 text-green-500" />,
  failed: <XCircle className="w-4 h-4 text-red-500" />,
  running: <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />,
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  return `${days} 天前`
}

function formatTokens(n: number): string {
  if (!n) return ''
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function ReviewTimeline({ runs }: { runs: ReviewRun[] }) {
  const [tokenMap, setTokenMap] = useState<Record<string, number>>({})

  useEffect(() => {
    if (!runs.length) return
    Promise.all(
      runs.map(async (run) => {
        try {
          const t = await tokens.byRun(run.run_id)
          return [run.run_id, t.total_tokens || 0] as [string, number]
        } catch {
          return [run.run_id, 0] as [string, number]
        }
      })
    ).then((pairs) => setTokenMap(Object.fromEntries(pairs)))
  }, [runs])

  if (!runs.length) {
    return (
      <div className="text-center py-8 text-gray-400">
        暂无审查记录
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {runs.map((run) => {
        const tok = tokenMap[run.run_id]
        return (
          <Link
            key={run.run_id}
            to={`/reviews/${run.run_id}`}
            className="flex items-center gap-4 p-4 rounded-lg border border-gray-200 hover:border-brand-300 hover:shadow-sm transition-all group"
          >
            <div className="shrink-0">
              {STATUS_ICON[run.status]}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <GitPullRequest className="w-4 h-4 text-gray-400" />
                <span className="font-medium text-sm truncate">{run.repo}</span>
                <span className="text-sm text-gray-500">#{run.pr_number}</span>
              </div>
              <p className="text-xs text-gray-400 mt-0.5 font-mono truncate">
                {run.head_sha?.slice(0, 8) || '—'}
              </p>
            </div>
            <div className="text-right shrink-0 flex flex-col items-end gap-1">
              <div className="text-sm font-medium text-gray-700">
                {run.summary?.confirmed ?? 0} / {run.summary?.total_findings ?? 0} 确认
              </div>
              <div className="flex items-center gap-3 text-xs text-gray-400">
                {tok ? (
                  <span className="flex items-center gap-1 text-yellow-600">
                    <Zap className="w-3 h-3" />
                    {formatTokens(tok)}
                  </span>
                ) : null}
                <span>{timeAgo(run.started_at)}</span>
              </div>
            </div>
          </Link>
        )
      })}
    </div>
  )
}
