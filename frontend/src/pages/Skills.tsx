import { useEffect, useState } from 'react'
import { BookOpen, Plus, Trash2, Lock, Save, X } from 'lucide-react'
import { admin, type SkillMeta } from '../api/client'

const EMPTY = { name: '', reviewer_type: '', description: '', body: '', languages: '', frameworks: '' }

export default function Skills() {
  const [skills, setSkills] = useState<SkillMeta[]>([])
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    admin
      .listSkills()
      .then((r) => setSkills(r.skills))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))

  useEffect(() => {
    refresh()
  }, [])

  const startNew = () => {
    setForm(EMPTY)
    setEditing(true)
    setError('')
  }

  const startEdit = async (name: string) => {
    setError('')
    try {
      const s = await admin.getSkill(name)
      setForm({ name: s.name, reviewer_type: s.meta.reviewer_type, description: s.meta.description, body: s.body, languages: (s.meta.languages || []).join(', '), frameworks: (s.meta.frameworks || []).join(', ') })
      setEditing(true)
    } catch (e) {
      setError(String(e))
    }
  }

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      await admin.saveSkill({
        ...form,
        languages: form.languages ? form.languages.split(',').map((s) => s.trim()).filter(Boolean) : [],
        frameworks: form.frameworks ? form.frameworks.split(',').map((s) => s.trim()).filter(Boolean) : [],
      })
      setEditing(false)
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    if (!confirm(`删除 skill「${name}」？`)) return
    try {
      await admin.deleteSkill(name)
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  if (loading) return <div role="status" className="flex items-center justify-center h-64 text-gray-400">加载中...</div>

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Skills</h1>
          <p className="text-sm text-gray-500 mt-1">审查规则集（SKILL.md）。新增/编辑后下一次审查即生效，无需重启。</p>
        </div>
        <button onClick={startNew} className="inline-flex items-center gap-2 px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700">
          <Plus className="w-4 h-4" /> 新增 Skill
        </button>
      </div>

      {error && <div role="alert" className="bg-red-50 text-red-700 text-sm rounded-lg p-3">{error}</div>}

      {editing && (
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <span>{form.name ? `编辑：${form.name}` : '新增 Skill'}</span>
            <button onClick={() => setEditing(false)} className="text-gray-400 hover:text-gray-600">
              <X className="w-4 h-4" />
            </button>
          </div>
          <div className="card-body space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block text-sm">
                <span className="text-gray-600">名称（小写_下划线）</span>
                <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="compliance_rules" />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">reviewer_type（挂到哪个审查维度，可空）</span>
                <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.reviewer_type} onChange={(e) => setForm({ ...form, reviewer_type: e.target.value })} placeholder="compliance" />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">languages（逗号分隔，空=通用）</span>
                <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.languages} onChange={(e) => setForm({ ...form, languages: e.target.value })} placeholder="python, go" />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">frameworks（逗号分隔，空=不限）</span>
                <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.frameworks} onChange={(e) => setForm({ ...form, frameworks: e.target.value })} placeholder="react, next" />
              </label>
            </div>
            <label className="block text-sm">
              <span className="text-gray-600">描述</span>
              <input className="mt-1 w-full border rounded-lg px-3 py-2 text-sm" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </label>
            <label className="block text-sm">
              <span className="text-gray-600">规则正文（Markdown）</span>
              <textarea className="mt-1 w-full border rounded-lg px-3 py-2 text-sm font-mono h-56" value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} placeholder="## 检查要点&#10;1. ..." />
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
          <BookOpen className="w-5 h-5 text-brand-600" /> 已注册 Skills ({skills.length})
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {skills.map((s) => (
              <div key={s.name} className="p-4 border border-gray-200 rounded-lg">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span className="font-medium text-sm">{s.name}</span>
                  {s.reviewer_type && <span className="badge badge-gray text-xs">{s.reviewer_type}</span>}
                  {s.languages && s.languages.length > 0 && s.languages.map((l) => (
                    <span key={l} className="badge badge-info text-xs">{l}</span>
                  ))}
                  {s.frameworks && s.frameworks.length > 0 && s.frameworks.map((f) => (
                    <span key={f} className="badge badge-success text-xs">{f}</span>
                  ))}
                  {s.is_builtin && (
                    <span className="inline-flex items-center gap-1 text-xs text-gray-400">
                      <Lock className="w-3 h-3" /> 内置
                    </span>
                  )}
                </div>
                <p className="text-xs text-gray-500 leading-relaxed">{s.description || '—'}</p>
                {!s.is_builtin && (
                  <div className="flex gap-3 mt-2">
                    <button onClick={() => startEdit(s.name)} className="text-xs text-brand-600 hover:underline">编辑</button>
                    <button onClick={() => remove(s.name)} className="inline-flex items-center gap-1 text-xs text-red-600 hover:underline">
                      <Trash2 className="w-3 h-3" /> 删除
                    </button>
                  </div>
                )}
              </div>
            ))}
            {!skills.length && <span className="text-gray-400 text-sm">暂无 Skills</span>}
          </div>
        </div>
      </div>
    </div>
  )
}
