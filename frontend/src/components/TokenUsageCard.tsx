import { Zap } from 'lucide-react'

interface TokenUsageProps {
  totalTokens: number
  agents?: { agent_name: string; total_tokens: number }[]
  compact?: boolean
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

export default function TokenUsageCard({ totalTokens, agents, compact }: TokenUsageProps) {
  if (compact) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-600">
        <Zap className="w-4 h-4 text-yellow-500" />
        <span className="font-medium">{formatTokens(totalTokens)}</span>
        <span className="text-gray-400">tokens</span>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="card-header flex items-center gap-2">
        <Zap className="w-5 h-5 text-yellow-500" />
        Token 用量
      </div>
      <div className="card-body">
        <div className="text-3xl font-bold text-gray-900 mb-4">
          {formatTokens(totalTokens)}
          <span className="text-sm font-normal text-gray-500 ml-2">tokens</span>
        </div>
        {agents && agents.length > 0 && (
          <div className="space-y-2">
            {agents.map((a) => (
              <div key={a.agent_name} className="flex items-center justify-between">
                <span className="text-sm text-gray-600">{a.agent_name}</span>
                <div className="flex items-center gap-2">
                  <div className="w-24 bg-gray-100 rounded-full h-2">
                    <div
                      className="bg-brand-500 h-2 rounded-full"
                      style={{ width: `${Math.min(100, (a.total_tokens / totalTokens) * 100)}%` }}
                    />
                  </div>
                  <span className="text-sm font-medium text-gray-700 w-12 text-right">
                    {formatTokens(a.total_tokens)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
