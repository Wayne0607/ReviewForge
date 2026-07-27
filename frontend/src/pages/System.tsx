import { useEffect, useMemo, useState } from 'react'
import {
  Bot,
  Wrench,
  BookOpen,
  Settings,
  Plug,
  Cpu,
  KeyRound,
  Save,
  FlaskConical,
  RotateCcw,
  LoaderCircle,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'
import { admin, system } from '../api/client'
import type { LLMSettings, LLMSettingsPayload, LLMRolePayload, LLMRoleSettings } from '../api/client'
import type { SystemSpecs } from '../types'

// The five fixed functional roles.  Order matches ROLE_NAMES on the
// backend; keep them in sync.
const ROLE_LABELS: Record<string, { title: string; description: string }> = {
  planner: {
    title: 'Planner（规划）',
    description: '为每轮 PR 审查决定派哪些 Reviewer。',
  },
  fast_review: {
    title: 'Fast Review（快速）',
    description: '轻量级 reviewer：性能/风格/本地化/测试/文档/依赖/可访问性。',
  },
  deep_review: {
    title: 'Deep Review（深度）',
    description: '高资源 reviewer：安全/正确性/覆盖空缺审查。',
  },
  verifier: {
    title: 'Verifier（验证/校准）',
    description: '动态校准、Cross-PR、证据验证、Escalation 仲裁。',
  },
  publication_gate: {
    title: 'Publication Gate（发版前门）',
    description: '发布前对确认 finding 再次校验，独立模型避免复用 reviewer。',
  },
}

const ROLE_ORDER = ['planner', 'fast_review', 'deep_review', 'verifier', 'publication_gate']

type RoleFormState = {
  base_url: string
  model: string
  api_key: string
  reset: boolean
}

const emptyRoleForm = (): RoleFormState => ({ base_url: '', model: '', api_key: '', reset: false })

const roleFormFromSettings = (settings: LLMRoleSettings | undefined): RoleFormState => ({
  base_url: '',
  model: '',
  api_key: '',
  reset: false,
})

export default function System() {
  const [specs, setSpecs] = useState<SystemSpecs | null>(null)
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [llmSettings, setLLMSettings] = useState<LLMSettings | null>(null)
  const [llmForm, setLLMForm] = useState<LLMSettingsPayload>({
    base_url: '',
    model: '',
    fast_model: '',
    accurate_model: '',
    api_key: '',
  })
  const [roleForms, setRoleForms] = useState<Record<string, RoleFormState>>(() =>
    Object.fromEntries(ROLE_ORDER.map((name) => [name, emptyRoleForm()]))
  )
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [llmBusy, setLLMBusy] = useState<'test' | 'save' | 'reset' | null>(null)
  const [llmMessage, setLLMMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([system.specs(), system.config(), admin.getLLMSettings()])
      .then(([s, c, llm]) => {
        setSpecs(s)
        setConfig(c)
        applyLLMSettings(llm)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  const applyLLMSettings = (settings: LLMSettings) => {
    setLLMSettings(settings)
    setLLMForm({
      base_url: settings.base_url,
      model: settings.model,
      fast_model: settings.fast_model,
      accurate_model: settings.accurate_model,
      api_key: '',
    })
    // Reset per-role forms: blank inputs mean "preserve the existing
    // override, otherwise fall back to the global config".
    setRoleForms(
      Object.fromEntries(
        ROLE_ORDER.map((name) => [name, roleFormFromSettings(settings.roles?.[name])])
      )
    )
  }

  const llmError = (error: unknown) =>
    error instanceof Error ? error.message.replace(/^API \d+:\s*/, '') : '操作失败'

  const updateRoleForm = (name: string, patch: Partial<RoleFormState>) => {
    setRoleForms((value) => ({ ...value, [name]: { ...value[name], ...patch } }))
  }

  const buildPayload = (): LLMSettingsPayload => {
    const roles: Record<string, LLMRolePayload> = {}
    for (const name of ROLE_ORDER) {
      const form = roleForms[name]
      // Only emit a role entry when at least one field is non-blank.
      if (form.reset) {
        roles[name] = { reset: true }
      } else if (form.base_url || form.model || form.api_key) {
        roles[name] = {
          base_url: form.base_url,
          model: form.model,
          api_key: form.api_key,
        }
      }
    }
    return { ...llmForm, roles }
  }

  const testConnection = async () => {
    setLLMBusy('test')
    setLLMMessage(null)
    try {
      const result = await admin.testLLMSettings(buildPayload())
      const models = result.tested_models?.length ?? 0
      setLLMMessage({
        ok: true,
        text: `连接成功，累计延迟 ${result.latency_ms} ms，校验 ${models} 个有效端点`,
      })
    } catch (error) {
      setLLMMessage({ ok: false, text: llmError(error) })
    } finally {
      setLLMBusy(null)
    }
  }

  const saveSettings = async () => {
    setLLMBusy('save')
    setLLMMessage(null)
    try {
      const result = await admin.saveLLMSettings(buildPayload())
      applyLLMSettings(result.settings)
      setConfig((value) =>
        value ? { ...value, llm: { model: result.settings.model, base_url: result.settings.base_url } } : value
      )
      setLLMMessage({ ok: true, text: `已加密保存并热切换，连接延迟 ${result.connection.latency_ms} ms` })
    } catch (error) {
      setLLMMessage({ ok: false, text: llmError(error) })
    } finally {
      setLLMBusy(null)
      // After a save, blank out any keys the operator typed so the next
      // round does not accidentally re-send the same secret.
      setLLMForm((value) => ({ ...value, api_key: '' }))
      setRoleForms((value) =>
        Object.fromEntries(
          ROLE_ORDER.map((name) => [name, { ...value[name], api_key: '' }])
        )
      )
    }
  }

  const resetSettings = async () => {
    if (!window.confirm('恢复启动配置？控制台保存的模型配置将被删除。')) return
    setLLMBusy('reset')
    setLLMMessage(null)
    try {
      const result = await admin.resetLLMSettings()
      applyLLMSettings(result.settings)
      setConfig((value) =>
        value ? { ...value, llm: { model: result.settings.model, base_url: result.settings.base_url } } : value
      )
      setLLMMessage({ ok: true, text: '已恢复启动配置，后续审查将使用该配置' })
    } catch (error) {
      setLLMMessage({ ok: false, text: llmError(error) })
    } finally {
      setLLMBusy(null)
    }
  }

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

  const anyRoleDirty = useMemo(
    () =>
      ROLE_ORDER.some(
        (name) =>
          roleForms[name]?.base_url ||
          roleForms[name]?.model ||
          roleForms[name]?.api_key ||
          roleForms[name]?.reset
      ),
    [roleForms]
  )

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">系统信息</h1>
        <p className="text-sm text-gray-500 mt-1">
          ReviewForge 注册的 Agents、Tools、Skills 和当前配置
        </p>
      </div>

      {/* LLM settings */}
      {llmSettings && (
        <div className="card">
          <div className="card-header flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <KeyRound className="w-5 h-5 text-brand-600" />
              模型服务
            </div>
            <span className={`badge ${llmSettings.source === 'console' ? 'badge-info' : 'badge-gray'}`}>
              {llmSettings.source === 'console' ? '控制台配置' : '启动配置'}
            </span>
          </div>
          <div className="card-body space-y-4">
            <div className="rounded-lg bg-blue-50 px-4 py-3 text-sm text-blue-800">
              配置会加密保存在服务器，仅影响保存后的新审查；正在运行的任务继续使用原配置。
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <label className="space-y-1.5 lg:col-span-2">
                <span className="text-sm font-medium text-gray-700">OpenAI 兼容 Base URL</span>
                <input
                  value={llmForm.base_url}
                  onChange={(event) => setLLMForm({ ...llmForm, base_url: event.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                  placeholder="https://api.example.com/v1"
                  spellCheck={false}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-sm font-medium text-gray-700">默认模型</span>
                <input
                  value={llmForm.model}
                  onChange={(event) => setLLMForm({ ...llmForm, model: event.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                  spellCheck={false}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-sm font-medium text-gray-700">API Key</span>
                <input
                  type="password"
                  value={llmForm.api_key}
                  onChange={(event) => setLLMForm({ ...llmForm, api_key: event.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                  placeholder={
                    llmSettings.api_key_configured
                      ? `留空则保持当前密钥（末四位 ${llmSettings.api_key_last4}）`
                      : '请输入 API Key'
                  }
                  autoComplete="new-password"
                  spellCheck={false}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-sm font-medium text-gray-700">快速任务模型</span>
                <input
                  value={llmForm.fast_model}
                  onChange={(event) => setLLMForm({ ...llmForm, fast_model: event.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                  placeholder="留空则使用启动配置"
                  spellCheck={false}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-sm font-medium text-gray-700">高精度任务模型</span>
                <input
                  value={llmForm.accurate_model}
                  onChange={(event) => setLLMForm({ ...llmForm, accurate_model: event.target.value })}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                  placeholder="留空则使用启动配置"
                  spellCheck={false}
                />
              </label>
            </div>

            {/* Advanced per-role routing */}
            <div className="border border-gray-200 rounded-lg">
              <button
                type="button"
                onClick={() => setAdvancedOpen((value) => !value)}
                className="w-full flex items-center justify-between px-4 py-3 text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                <div className="flex items-center gap-2">
                  {advancedOpen ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  高级：按角色配置模型（5 个固定角色）
                </div>
                {anyRoleDirty && <span className="badge badge-warning text-xs">未保存的修改</span>}
              </button>
              {advancedOpen && (
                <div className="border-t border-gray-200 p-4 space-y-4">
                  <p className="text-xs text-gray-500">
                    留空字段表示沿用全局 Base URL / API Key / 默认模型。每个角色可独立指向另一家服务商。
                    当前生效配置（不包含密钥）展示在下方，仅供确认。
                  </p>
                  {ROLE_ORDER.map((name) => {
                    const meta = ROLE_LABELS[name]
                    const settings = llmSettings.roles?.[name]
                    return (
                      <div key={name} className="rounded-lg border border-gray-100 bg-gray-50/50 p-3 space-y-2">
                        <div className="flex items-center justify-between">
                          <div>
                            <div className="text-sm font-medium text-gray-800">{meta.title}</div>
                            <div className="text-xs text-gray-500">{meta.description}</div>
                          </div>
                          {settings && (
                            <div className="text-right text-xs text-gray-500 leading-tight">
                              <div>当前 base URL：{settings.base_url || '(同全局)'}</div>
                              <div>当前模型：{settings.model || '(同全局)'}</div>
                              <div>
                                API Key：
                                {settings.api_key_configured
                                  ? `已配置（末四位 ${settings.api_key_last4}）`
                                  : '沿用全局'}
                              </div>
                            </div>
                          )}
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                          <input
                            value={roleForms[name]?.base_url ?? ''}
                            onChange={(event) => updateRoleForm(name, { base_url: event.target.value })}
                            placeholder="Base URL（留空 = 沿用全局）"
                            className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                            spellCheck={false}
                            disabled={roleForms[name]?.reset}
                          />
                          <input
                            value={roleForms[name]?.model ?? ''}
                            onChange={(event) => updateRoleForm(name, { model: event.target.value })}
                            placeholder="模型名（留空 = 沿用全局）"
                            className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                            spellCheck={false}
                            disabled={roleForms[name]?.reset}
                          />
                          <input
                            type="password"
                            value={roleForms[name]?.api_key ?? ''}
                            onChange={(event) => updateRoleForm(name, { api_key: event.target.value })}
                            placeholder={
                              settings?.api_key_configured
                                ? `API Key（留空保留旧值，末四位 ${settings.api_key_last4}）`
                                : 'API Key（留空 = 沿用全局）'
                            }
                            autoComplete="new-password"
                            className="rounded-lg border border-gray-300 px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100"
                            spellCheck={false}
                            disabled={roleForms[name]?.reset}
                          />
                        </div>
                        <button
                          type="button"
                          className="text-xs text-red-600 hover:text-red-700"
                          onClick={() =>
                            updateRoleForm(name, {
                              base_url: '',
                              model: '',
                              api_key: '',
                              reset: !roleForms[name]?.reset,
                            })
                          }
                        >
                          {roleForms[name]?.reset ? '取消恢复' : '恢复该角色为全局配置'}
                        </button>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            {llmMessage && (
              <div
                className={`rounded-lg px-4 py-3 text-sm ${
                  llmMessage.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
                }`}
              >
                {llmMessage.text}
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn btn-ghost border border-gray-200"
                onClick={testConnection}
                disabled={llmBusy !== null}
              >
                {llmBusy === 'test' ? (
                  <LoaderCircle className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <FlaskConical className="w-4 h-4 mr-2" />
                )}
                测试连接
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={saveSettings}
                disabled={llmBusy !== null}
              >
                {llmBusy === 'save' ? (
                  <LoaderCircle className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <Save className="w-4 h-4 mr-2" />
                )}
                测试并保存
              </button>
              <button
                type="button"
                className="btn btn-ghost text-red-600"
                onClick={resetSettings}
                disabled={llmBusy !== null || llmSettings.source !== 'console'}
              >
                {llmBusy === 'reset' ? (
                  <LoaderCircle className="w-4 h-4 mr-2 animate-spin" />
                ) : (
                  <RotateCcw className="w-4 h-4 mr-2" />
                )}
                恢复启动配置
              </button>
            </div>
          </div>
        </div>
      )}

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
