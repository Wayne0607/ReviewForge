import { useEffect, useState } from 'react'
import { Bot, Plus, Trash2, Lock, Save, X } from 'lucide-react'
import { admin, type CustomAgent, type BuiltinAgent } from '../api/client'

const EMPTY = { reviewer_type: '', description: '', allowed_tools: [] as string[], max_steps: 6, instructions: '', enabled: true }

export default function Agents() {
  const [custom, setCustom] = useState<CustomAgent[]>([])
  const [builtin, setBuiltin] = useState<BuiltinAgent[]>([])
  const [tools, setTools] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    admin
      .listAgents()
      .then((r) => {
        setCustom(r.custom)
        setBuiltin(r.builtin)
        setTools(r.available_tools)
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))

  useEffect(() => {
    refresh()
  }, [])

  const startNew = () => {
    setForm({ ...EMPTY, allowed_tools: tools.filter((t) => ['read_diff', 'read_file', 'search_code', 'post_comment'].includes(t)) })
    setEditing(true)
    setError('')
  }

  const startEdit = (a: CustomAgent) => {
    setForm({ reviewer_type: a.reviewer_type, description: a.description, allowed_tools: a.allowed_tools, max_steps: a.max_steps, instructions: a.instructions, enabled: a.enabled })
    setEditing(true)
    setError('')
  }

  const toggleTool = (t: string) =>
    setForm((f) => ({ ...f, allowed_tools: f.allowed_tools.includes(t) ? f.allowed_tools.filter((x) => x !== t) : [...f.allowed_tools, t] }))

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      await admin.saveAgent(form)
      setEditing(false)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (rtype: string) => {
    if (!confirm(`删除 agent「${rtype}_reviewer」？`)) return
    try {
      await admin.deleteAgent(rtype)
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  if (loading) return <div className="flex items-center justify-center h-64 text-gray-400">加载中...</div>

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Agents</h1>
          <p className="text-sm text-gray-500 mt-1">配置型审查 Agent（纯配置，不写代码）。保存后下一次审查即生效。</p>
        </div>
        <button onClick={startNew} className="inline-flex items-center gap-2 px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700">
          <Plus className="w-4 h-4" /> 新增 Agent
        </button>
      </div>

      {error && <div className="bg-red-50 text-red-700 text-sm rounded-lg p-3">{error}</div>}

      {editing && (
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <span>{form.reviewer_type ? `编辑：${form.reviewer_type}_reviewer` : '新增 Agent'}</span>
            <button onClick={() => setEditing(false)} className="text-gray-400 hover:text-gray-600">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="card-body space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-gray-600">reviewer_type（小写_下划线，会生成 &lt;type&gt;_reviewer）</span>
                <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.reviewer_type} onChange={(e) => setForm({ ...form, reviewer_type: e.target.value })} placeholder="compliance" />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">max_steps（1-20）</span>
                <input type="number" min={1} max={20} className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.max_steps} onChange={(e) => setForm({ ...form, max_steps: Number(e.target.value) })} />
              </label>
            </div>
            <label className="block text-sm">
              <span className="text-gray-600">描述（Planner 用它决定是否启用本 Agent）</span>
              <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="检查数据合规：PII 处理、日志脱敏" />
            </label>
            <div className="text-sm">
              <span className="text-gray-600">可用工具</span>
              <div className="flex flex-wrap gap-2 mt-1">
                {tools.map((t) => (
                  <button key={t} type="button" onClick={() => toggleTool(t)} className={`px-3 py-1.5 rounded-lg text-xs border ${form.allowed_tools.includes(t) ? 'bg-brand-50 border-brand-300 text-brand-700' : 'border-gray-200 text-gray-500'}`}>
                    {t}
                  </button>
                ))}
              </div>
            </div>
            <label className="block text-sm">
              <span className="text-gray-600">审查规则 / instructions（注入到该 Agent 的 prompt）</span>
              <textarea className="mt-1 w-full border rounded-lg px-3 py-2 text-sm font-mono h-44" value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} placeholder="重点检查：&#10;1. 是否记录了未脱敏的 PII&#10;2. ..." />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              <span className="text-gray-600">启用</span>
            </label>
            <div className="flex justify-end">
              <button disabled={busy} onClick={save} className="inline-flex items-center gap-2 px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700 disabled:opacity-50">
                <Save className="w-4 h-4" /> {busy ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-header flex items-center gap-2">
          <Bot className="w-5 h-5 text-brand-600" /> 配置型 Agents ({custom.length})
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {custom.map((a) => (
              <div key={a.reviewer_type} className="p-4 border border-gray-200 rounded-lg">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-sm">{a.name}</span>
                  {!a.enabled && <span className="badge badge-gray text-xs">已停用</span>}
                </div>
                <p className="text-xs text-gray-500 leading-relaxed">{a.description}</p>
                <div className="text-xs text-gray-400 mt-1">工具: {a.allowed_tools.join(', ') || '—'} · max_steps {a.max_steps}</div>
                <div className="flex gap-3 mt-2">
                  <button onClick={() => startEdit(a)} className="text-xs text-brand-600 hover:underline">编辑</button>
                  <button onClick={() => remove(a.reviewer_type)} className="inline-flex items-center gap-1 text-xs text-red-600 hover:underline">
                    <Trash2 className="w-3 h-3" /> 删除
                  </button>
                </div>
              </div>
            ))}
            {!custom.length && <span className="text-gray-400 text-sm">还没有配置型 Agent，点右上角新增。</span>}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header flex items-center gap-2">
          <Lock className="w-5 h-5 text-gray-400" /> 内置 Agents ({builtin.length})
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {builtin.map((a) => (
              <div key={a.name} className="p-3 bg-gray-50 rounded-lg">
                <div className="font-medium text-sm">{a.name}</div>
                <div className="text-xs text-gray-500">{a.description}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
