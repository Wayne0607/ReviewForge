import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import type { WeeklyTrend } from '../types'

export default function TrendChart({ data }: { data: WeeklyTrend[] }) {
  if (!data.length) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        暂无趋势数据
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
        <XAxis dataKey="week" tick={{ fontSize: 12 }} />
        <YAxis tick={{ fontSize: 12 }} />
        <Tooltip
          contentStyle={{ borderRadius: '8px', border: '1px solid #e5e7eb' }}
        />
        <Legend />
        <Line
          type="monotone"
          dataKey="total"
          name="总发现"
          stroke="#3b82f6"
          strokeWidth={2}
          dot={{ r: 4 }}
        />
        <Line
          type="monotone"
          dataKey="confirmed"
          name="已确认"
          stroke="#10b981"
          strokeWidth={2}
          dot={{ r: 4 }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}
