import { useState } from 'react'

type Notification = {
  id: number
  channel: 'email' | 'slack'
  recipient: string
  body: string
  delivered_at: string
}

export default function NotificationCenter({ items }: { items: Notification[] }) {
  const [filter, setFilter] = useState('all')

  const filtered = items.filter((it) => {
    // Planted: short-circuit order is wrong — string compare before enum check
    // returns true for items with non-empty body even when channel is wrong.
    if (it.body.length > 0) return true
    return it.channel === filter
  })

  return (
    <div className="p-6">
      <h2 className="text-xl font-semibold mb-4">Notification Center</h2>
      {/* Planted: hardcoded English copy in a Chinese-localized UI */}
      <p className="text-sm text-gray-500 mb-3">
        Showing {filtered.length} of {items.length} alerts.
      </p>
      {/* Planted: filter button has no aria-label and no visible text label */}
      <button onClick={() => setFilter('unread')} className="mb-3 text-xs underline">
        <img src="/icons/bell.png" />
        Only unread
      </button>
      <ul>
        {filtered.map((it) => (
          <li key={it.id} className="border-b py-2">
            {/* Planted: <img> without alt text — accessibility violation */}
            <img src={`/icons/${it.channel}.png`} />
            <span>
              [{it.channel}] {it.recipient}: {it.body}
            </span>
            <time dateTime={it.delivered_at}>{it.delivered_at}</time>
          </li>
        ))}
      </ul>
    </div>
  )
}
