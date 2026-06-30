import { ReactNode, useState } from 'react'
import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  GitPullRequest,
  BarChart3,
  Server,
  Shield,
  BookOpen,
  Bot,
  KeyRound,
} from 'lucide-react'
import { TOKEN_KEY } from '../api/client'

const NAV_ITEMS = [
  { to: '/', label: '总览', icon: LayoutDashboard },
  { to: '/reviews', label: '审查记录', icon: GitPullRequest },
  { to: '/analytics', label: '趋势分析', icon: BarChart3 },
  { to: '/skills', label: 'Skills', icon: BookOpen },
  { to: '/agents', label: 'Agents', icon: Bot },
  { to: '/system', label: '系统信息', icon: Server },
]

export default function Layout({ children }: { children: ReactNode }) {
  const [token, setToken] = useState(() =>
    typeof localStorage !== 'undefined' ? localStorage.getItem(TOKEN_KEY) ?? '' : ''
  )
  const saveToken = () => {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
    window.location.reload() // refetch everything with the new header
  }
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 bg-gray-900 text-white flex flex-col shrink-0">
        {/* Logo */}
        <div className="flex items-center gap-3 px-6 py-5 border-b border-gray-700">
          <Shield className="w-7 h-7 text-brand-400" />
          <div>
            <h1 className="text-lg font-bold tracking-tight">ReviewForge</h1>
            <p className="text-xs text-gray-400">AI Code Review</p>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-brand-600 text-white'
                    : 'text-gray-300 hover:bg-gray-800 hover:text-white'
                }`
              }
            >
              <Icon className="w-5 h-5 shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* API token (stored in this browser only; not baked into the build) */}
        <div className="px-4 py-3 border-t border-gray-700">
          <label htmlFor="rf-token" className="flex items-center gap-1.5 text-xs text-gray-400 mb-1">
            <KeyRound className="w-3.5 h-3.5" /> API Token
          </label>
          <div className="flex gap-1">
            <input
              id="rf-token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && saveToken()}
              placeholder="粘贴 REVIEWFORGE_API_TOKEN"
              className="flex-1 min-w-0 bg-gray-800 text-gray-100 text-xs rounded px-2 py-1 border border-gray-700 focus:border-brand-500 outline-none"
            />
            <button onClick={saveToken} className="text-xs px-2.5 py-1 bg-brand-600 hover:bg-brand-700 rounded text-white shrink-0">
              保存
            </button>
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-700 text-xs text-gray-500">
          <p>ReviewForge v0.2.0</p>
          <p className="mt-1">Multi-Agent Code Review</p>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-gray-50">
        <div className="max-w-7xl mx-auto px-6 py-6">
          {children}
        </div>
      </main>
    </div>
  )
}
