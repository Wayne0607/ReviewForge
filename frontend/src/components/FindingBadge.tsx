import type { Finding } from '../types'

const SEVERITY_MAP = {
  error: { label: '严重', cls: 'badge-error' },
  warning: { label: '警告', cls: 'badge-warning' },
  info: { label: '信息', cls: 'badge-info' },
}

const STATUS_MAP = {
  confirmed: { label: '已确认', cls: 'badge-success' },
  false_positive: { label: '误报', cls: 'badge-gray' },
  candidate: { label: '待验证', cls: 'badge-warning' },
  reported: { label: '已评论', cls: 'badge-info' },
}

export default function FindingBadge({ finding }: { finding: Finding }) {
  const sev = SEVERITY_MAP[finding.severity] || SEVERITY_MAP.info
  const sta = STATUS_MAP[finding.status] || STATUS_MAP.candidate

  return (
    <div className="border border-gray-200 rounded-lg p-4 hover:border-brand-300 transition-colors">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className={`badge ${sev.cls}`}>{sev.label}</span>
            <span className={`badge ${sta.cls}`}>{sta.label}</span>
            <span className="badge badge-gray">{finding.category}</span>
            <span className="text-xs text-gray-400">{finding.reviewer}</span>
          </div>
          <p className="text-sm font-mono text-gray-500 truncate">
            {finding.file}:{finding.line}
          </p>
          <p className="mt-1 text-sm text-gray-800">{finding.message}</p>
          {finding.suggestion && (
            <p className="mt-1 text-sm text-gray-500 italic">💡 {finding.suggestion}</p>
          )}
        </div>
        <div className="text-right shrink-0">
          <div className="text-lg font-bold text-gray-700">
            {Math.round(finding.confidence * 100)}%
          </div>
          <div className="text-xs text-gray-400">置信度</div>
        </div>
      </div>
    </div>
  )
}
