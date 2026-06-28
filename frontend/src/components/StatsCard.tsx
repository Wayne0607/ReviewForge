import { ReactNode } from 'react'

interface StatsCardProps {
  title: string
  value: string | number
  subtitle?: string
  icon: ReactNode
  trend?: 'up' | 'down' | 'neutral'
}

export default function StatsCard({ title, value, subtitle, icon, trend }: StatsCardProps) {
  return (
    <div className="card">
      <div className="card-body flex items-start justify-between">
        <div>
          <p className="text-sm font-medium text-gray-500">{title}</p>
          <p className="mt-1 text-3xl font-bold text-gray-900">{value}</p>
          {subtitle && (
            <p className={`mt-1 text-sm ${
              trend === 'up' ? 'text-green-600' : trend === 'down' ? 'text-red-600' : 'text-gray-500'
            }`}>
              {subtitle}
            </p>
          )}
        </div>
        <div className="p-3 bg-brand-50 rounded-xl text-brand-600">
          {icon}
        </div>
      </div>
    </div>
  )
}
