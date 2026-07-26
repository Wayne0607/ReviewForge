import { useEffect, useState } from 'react'
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
} from 'lucide-react'
import { admin, system } from '../api/client'
import type { LLMSettings, LLMSettingsPayload } from '../api/client'
import type { SystemSpecs } from '../types'

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
  }

  const llmError = (error: unknown) =>
    error instanceof Error ? error.message.replace(/^API \d+:\s*/, '') : '操作失败'

  const testConnection = async () => {
    setLLMBusy('test')
    setLLMMessage(null)
    try {
      const result = await admin.testLLMSettings(llmForm)
      setLLMMessage({ ok: true, text: `连接成功，延迟 ${result.latency_ms} ms` })
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
      const result = await admin.saveLLMSettings(llmForm)
      applyLLMSettings(result.settings)
      setConfig((value) =>
        value ? { ...value, llm: { model: result.settings.model, base_url: result.settings.base_url } } : value
      )
      setLLMMessage({ ok: true, text: `已加密保存并热切换，连接延迟 ${result.connection.latency_ms} ms` })
    } catch (error) {
      setLLMMessage({ ok: false, text: llmError(error) })
    } finally {
      setLLMBusy(null)
      setLLMForm((value) => ({ ...value, api_key: '' }))
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
