import { useEffect, useState } from 'react'
import {
  Bot,
  Wrench,
  BookOpen,
  Settings,
  Plug,
  Cpu,
} from 'lucide-react'
import { system } from '../api/client'
import type { SystemSpecs } from '../types'

export default function System() {
  const [specs, setSpecs] = useState<SystemSpecs | null>(null)
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([system.specs(), system.config()])
      .then(([s, c]) => {
        setSpecs(s)
        setConfig(c)
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

  const agents = specs?.agents ?? {}
  const tools = specs?.tools ?? {}
  const skills = specs?.skills ?? []

  // Separate built-in and plugin reviewers
  const builtInAgents = Object.entries(agents).filter(
    ([name]) => !name.startsWith('plugin_')
  )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">系统信息</h1>
        <p className="text-sm text-gray-500 mt-1">
          ReviewForge 注册的 Agents、Tools、Skills 和当前配置
        </p>
      </div>

      {/* Agents */}
      <div className="card">
        <div className="card-header flex items-center gap-2">
          <Bot className="w-5 h-5 text-brand-600" />
          注册的 Agents ({builtInAgents.length})
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {builtInAgents.map(([name, spec]) => (
              <div
                key={name}
                className="p-4 border border-gray-200 rounded-lg hover:border-brand-300 transition-colors"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-sm">{name}</span>
                  <span className="badge badge-gray text-xs">{spec.role}</span>
                </div>
                <p className="text-xs text-gray-500 leading-relaxed">
                  {spec.description}
                </p>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Tools */}
      <div className="card">
        <div className="card-header flex items-center gap-2">
          <Wrench className="w-5 h-5 text-brand-600" />
          注册的 Tools ({Object.keys(tools).length})
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {Object.entries(tools).map(([name, tool]) => (
              <div
                key={name}
                className="flex items-start gap-3 p-3 bg-gray-50 rounded-lg"
              >
                <Cpu className="w-4 h-4 text-gray-400 mt-0.5 shrink-0" />
                <div>
                  <div className="font-medium text-sm">{name}</div>
                  <div className="text-xs text-gray-500">{tool.description}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Skills */}
      <div className="card">
        <div className="card-header flex items-center gap-2">
          <BookOpen className="w-5 h-5 text-brand-600" />
          注册的 Skills ({skills.length})
        </div>
        <div className="card-body">
          <div className="flex flex-wrap gap-2">
            {skills.map((s) => (
              <span
                key={s}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand-50 text-brand-700 rounded-lg text-sm"
              >
                <Plug className="w-3.5 h-3.5" />
                {s}
              </span>
            ))}
            {!skills.length && (
              <span className="text-gray-400 text-sm">暂无 Skills</span>
            )}
          </div>
        </div>
      </div>

      {/* Config */}
      {config && (
        <div className="card">
          <div className="card-header flex items-center gap-2">
            <Settings className="w-5 h-5 text-brand-600" />
            当前配置
          </div>
          <div className="card-body">
            <pre className="text-sm bg-gray-50 rounded-lg p-4 overflow-x-auto font-mono text-gray-700">
              {JSON.stringify(config, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}
