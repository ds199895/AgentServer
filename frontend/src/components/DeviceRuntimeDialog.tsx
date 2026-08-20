import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Copy, Cpu, LoaderCircle, Play, Radio, Send, ShieldOff, Square } from 'lucide-react'

import {
  api,
  isAbortError,
  isRuntimeEventForSession,
  isRuntimeSessionForDevice,
  runtimeRequestIdentityFor,
  type Device,
  type DeviceRuntimeEnrollment,
  type RuntimeEvent,
  type RuntimeRequestIdentity,
  type RuntimeSession,
} from '@/api'
import { Button } from '@/components/ui/button'
import { bootstrapCurlProtocolArgs } from '@/device-bootstrap'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

type Props = {
  device: Device
  onClose: () => void
  onChanged: () => void
}

function runtimeLabel(device: Device): string {
  switch (device.runtime?.state) {
    case 'online': return '在线'
    case 'degraded': return '降级'
    case 'offline': return '离线'
    case 'revoked': return '已撤销'
    default: return '未配对'
  }
}

type RuntimeQuestion = {
  id: string
  header?: string
  question?: string
  options?: Array<{ label: string; description?: string }>
}

type PendingTurnRequest = RuntimeRequestIdentity & {
  sessionId: string
  input: string
  baselineRevision: number | null
  baselineUpdatedAt: number
}

function interactionId(event: RuntimeEvent): string {
  const value = event.payload.interaction_id || event.payload.request_id || event.interaction_id
  return typeof value === 'string' ? value : ''
}

function eventTime(event: RuntimeEvent): string {
  const value = event.occurred_at ?? event.recorded_at
  return Number.isFinite(value) ? new Date(value * 1000).toLocaleTimeString() : ''
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`
}

export function DeviceRuntimeDialog({ device, onClose, onChanged }: Props) {
  const [enrollment, setEnrollment] = useState<DeviceRuntimeEnrollment | null>(null)
  const [sessions, setSessions] = useState<RuntimeSession[]>([])
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [cwd, setCwd] = useState('.')
  const [model, setModel] = useState('')
  const [turnInput, setTurnInput] = useState('')
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [permissionMode, setPermissionMode] = useState<RuntimeSession['permission_mode']>('workspace-write')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [sessionsError, setSessionsError] = useState('')
  const [eventsError, setEventsError] = useState('')
  const [copied, setCopied] = useState(false)
  const mountedRef = useRef(true)
  const deviceIdRef = useRef(device.id)
  const controllersRef = useRef(new Set<AbortController>())
  const copyTimerRef = useRef<number | null>(null)
  const pendingSessionCreateRef = useRef<{ fingerprint: string; requestId: string } | null>(null)
  const pendingTurnRef = useRef<PendingTurnRequest | null>(null)
  deviceIdRef.current = device.id

  const beginRequest = useCallback(() => {
    const controller = new AbortController()
    controllersRef.current.add(controller)
    return controller
  }, [])
  const endRequest = useCallback((controller: AbortController) => {
    controllersRef.current.delete(controller)
  }, [])
  const abortAllRequests = useCallback(() => {
    for (const controller of controllersRef.current) controller.abort()
    controllersRef.current.clear()
  }, [])
  const isCurrentRequest = useCallback((controller: AbortController, expectedDeviceId: string) => (
    mountedRef.current && !controller.signal.aborted && deviceIdRef.current === expectedDeviceId
  ), [])

  const registered = Boolean(device.runtime && device.runtime.state !== 'unregistered' && device.runtime.state !== 'revoked')
  const providers = useMemo(
    () => Array.isArray(device.runtime?.providers) ? device.runtime.providers : [],
    [device.runtime?.providers],
  )
  const codexProvider = providers.find((provider) => provider.id === 'codex')
  const canStartCodex = Boolean(
    codexProvider?.available
    && (device.runtime?.state === 'online' || device.runtime?.state === 'degraded'),
  )
  const providerSummary = useMemo(
    () => providers.map((provider) => `${provider.id}${provider.available ? '' : '（不可用）'}`).join('、') || '尚未上报',
    [providers],
  )
  const installCommand = useMemo(() => [
    `curl --fail --silent --show-error ${bootstrapCurlProtocolArgs(window.location.origin)} -o install-agentserver-device.sh ${shellQuote(`${window.location.origin}/device-bootstrap/install.sh`)} && \\`,
    'bash install-agentserver-device.sh \\',
    `  --device-id ${shellQuote(device.id)} \\`,
    `  --base-url ${shellQuote(window.location.origin)} \\`,
    `  --remote-port ${device.remote_port} \\`,
    `  --ssh-user ${shellQuote(device.ssh_user)} \\`,
    `  --runtime-bundle-url ${shellQuote(window.location.origin)}`,
 ].join('\n'), [device.id, device.remote_port, device.ssh_user])
  const selectedSession = useMemo(
    () => sessions.find((session) => (
      session.id === selectedSessionId && isRuntimeSessionForDevice(session, device.id)
    )) ?? null,
    [device.id, selectedSessionId, sessions],
  )
  const activeInteraction = useMemo(() => {
    const requestId = selectedSession?.active_request_id
    if (!requestId) return null
    return [...events].reverse().find((event) => (
      ['interaction.opened', 'request.opened', 'user-input.requested'].includes(event.type)
      && interactionId(event) === requestId
    )) ?? null
  }, [events, selectedSession?.active_request_id])
  const questions = useMemo(() => {
    const value = activeInteraction?.payload.questions
    if (!Array.isArray(value)) return []
    return value.filter((question): question is RuntimeQuestion => (
      Boolean(question) && typeof question === 'object' && typeof (question as RuntimeQuestion).id === 'string'
    ))
  }, [activeInteraction])

  useEffect(() => {
    const pending = pendingTurnRef.current
    if (!pending || !selectedSession || selectedSession.id !== pending.sessionId) return
    const revisionAdvanced = pending.baselineRevision !== null
      && typeof selectedSession.revision === 'number'
      && selectedSession.revision > pending.baselineRevision
    const updatedAdvanced = selectedSession.updated_at > pending.baselineUpdatedAt
    if (selectedSession.state === 'ready' && !revisionAdvanced && !updatedAdvanced) return
    pendingTurnRef.current = null
    setTurnInput((current) => current.trim() === pending.input ? '' : current)
  }, [selectedSession])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortAllRequests()
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current)
    }
  }, [abortAllRequests])

  useEffect(() => {
    setError('')
    setSessionsError('')
    setEventsError('')
    setCopied(false)
    if (registered) {
      // Drop the one-time secret as soon as the Host has consumed it.
      setEnrollment(null)
    } else {
      pendingSessionCreateRef.current = null
      pendingTurnRef.current = null
      setSessions([])
      setSelectedSessionId(null)
      setEvents([])
      setAnswers({})
    }
  }, [device.id, registered])

  useEffect(() => {
    if (!registered) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    let timer: number | null = null
    const refresh = async () => {
      try {
        const value = await api.runtimeSessions(expectedDeviceId, { signal: controller.signal })
        if (!isCurrentRequest(controller, expectedDeviceId)) return
        const returned = Array.isArray(value.sessions) ? value.sessions : []
        const scoped = returned.filter((session) => isRuntimeSessionForDevice(session, expectedDeviceId))
        if (pendingSessionCreateRef.current
          && scoped.some((session) => session.id === pendingSessionCreateRef.current?.requestId)) {
          pendingSessionCreateRef.current = null
        }
        setSessions(scoped)
        setSelectedSessionId((current) => (
          current && scoped.some((session) => session.id === current)
            ? current
            : scoped[0]?.id ?? null
        ))
        setSessionsError(returned.length === scoped.length
          ? ''
          : '服务器返回了其他设备的会话，已安全忽略。')
      } catch (reason) {
        if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
          setSessionsError(errorMessage(reason, '无法刷新 Runtime 会话'))
        }
      } finally {
        if (isCurrentRequest(controller, expectedDeviceId)) {
          timer = window.setTimeout(() => void refresh(), 1500)
        }
      }
    }
    void refresh()
    return () => {
      if (timer !== null) window.clearTimeout(timer)
      controller.abort()
      endRequest(controller)
    }
  }, [beginRequest, device.id, endRequest, isCurrentRequest, registered])

  useEffect(() => {
    const expectedDeviceId = device.id
    const sessionId = selectedSession?.id
    let cursor = 0
    let history: RuntimeEvent[] = []
    setEvents([])
    setAnswers({})
    setEventsError('')
    if (!sessionId) return
    const controller = beginRequest()
    let timer: number | null = null
    const refresh = async () => {
      try {
        const value = await api.runtimeSessionEvents(sessionId, cursor, { signal: controller.signal })
        if (!isCurrentRequest(controller, expectedDeviceId)) return
        const returned = Array.isArray(value.events) ? value.events : []
        const scoped = returned.filter((event) => (
          isRuntimeEventForSession(event, expectedDeviceId, sessionId)
          && Number.isFinite(event.sequence)
          && event.sequence >= 0
        ))
        setEventsError(returned.length === scoped.length
          ? ''
          : '服务器返回了不属于当前会话的事件，已安全忽略。')
        if (scoped.length) {
          const seen = new Set(history.map((event) => event.event_id))
          history = [...history, ...scoped.filter((event) => !seen.has(event.event_id))].slice(-200)
          cursor = Math.max(cursor, ...scoped.map((event) => event.sequence))
          setEvents(history)
        }
      } catch (reason) {
        if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
          setEventsError(errorMessage(reason, '无法刷新 Runtime 事件'))
        }
      } finally {
        if (isCurrentRequest(controller, expectedDeviceId)) {
          timer = window.setTimeout(() => void refresh(), 1000)
        }
      }
    }
    void refresh()
    return () => {
      if (timer !== null) window.clearTimeout(timer)
      controller.abort()
      endRequest(controller)
    }
  }, [beginRequest, device.id, endRequest, isCurrentRequest, selectedSession?.id])

  const closeDialog = () => {
    mountedRef.current = false
    abortAllRequests()
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current)
    copyTimerRef.current = null
    setEnrollment(null)
    setSessions([])
    setSelectedSessionId(null)
    setEvents([])
    setAnswers({})
    setTurnInput('')
    pendingSessionCreateRef.current = null
    pendingTurnRef.current = null
    setCopied(false)
    setBusy(false)
    onClose()
  }

  const pair = async () => {
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setEnrollment(null)
    setCopied(false)
    setBusy(true); setError('')
    try {
      const value = await api.createRuntimeEnrollment(expectedDeviceId, { signal: controller.signal })
      if (!isCurrentRequest(controller, expectedDeviceId)) return
      if (value.device_id !== expectedDeviceId || !value.enrollment_token) {
        throw new Error('配对响应与当前设备不匹配，凭据已丢弃。')
      }
      setEnrollment(value)
      onChanged()
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法创建配对凭据'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const probe = async () => {
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.probeDeviceRuntime(expectedDeviceId, { signal: controller.signal })
      if (isCurrentRequest(controller, expectedDeviceId)) onChanged()
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法探测 Runtime Host'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const revoke = async () => {
    if (!window.confirm(`撤销 ${device.name} 的 Runtime Host 凭据？运行中的原生 Agent 会话将停止接收控制命令。`)) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.revokeDeviceRuntime(expectedDeviceId, { signal: controller.signal })
      if (!isCurrentRequest(controller, expectedDeviceId)) return
      endRequest(controller)
      onChanged()
      closeDialog()
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法撤销 Runtime Host'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const startSession = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!canStartCodex) {
      setError('当前设备未上报可用的 Codex Provider。')
      return
    }
    const expectedDeviceId = device.id
    const controller = beginRequest()
    const normalizedCwd = cwd.trim() || '.'
    const normalizedModel = model.trim() || null
    const fingerprint = JSON.stringify([
      expectedDeviceId, 'codex', normalizedCwd, permissionMode, normalizedModel,
    ])
    pendingSessionCreateRef.current = runtimeRequestIdentityFor(
      pendingSessionCreateRef.current,
      fingerprint,
    )
    const requestId = pendingSessionCreateRef.current.requestId
    setBusy(true); setError('')
    try {
      const value = await api.createRuntimeSession(expectedDeviceId, {
        session_id: requestId,
        provider: 'codex', cwd: normalizedCwd, permission_mode: permissionMode,
        model: normalizedModel,
      }, { signal: controller.signal })
      if (!isCurrentRequest(controller, expectedDeviceId)) return
      if (value.session.id !== requestId || !isRuntimeSessionForDevice(value.session, expectedDeviceId)) {
        throw new Error('会话响应与当前设备或请求不匹配，已拒绝显示。')
      }
      pendingSessionCreateRef.current = null
      setSessions((current) => [value.session, ...current.filter((item) => item.id !== value.session.id)])
      setSelectedSessionId(value.session.id)
      onChanged()
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        if (pendingSessionCreateRef.current?.requestId === requestId) {
          setError(errorMessage(reason, '无法启动 Codex Runtime 会话'))
        }
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const startTurn = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    const session = selectedSession
    const input = turnInput.trim()
    if (!session || !isRuntimeSessionForDevice(session, device.id) || !input) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    const fingerprint = JSON.stringify([expectedDeviceId, session.id, input, session.model])
    const identity = runtimeRequestIdentityFor(pendingTurnRef.current, fingerprint)
    if (identity !== pendingTurnRef.current) {
      pendingTurnRef.current = {
        ...identity,
        sessionId: session.id,
        input,
        baselineRevision: typeof session.revision === 'number' ? session.revision : null,
        baselineUpdatedAt: session.updated_at,
      }
    }
    const requestId = pendingTurnRef.current.requestId
    setBusy(true); setError('')
    try {
      await api.startRuntimeTurn(session.id, input, session.model, requestId, { signal: controller.signal })
      if (!isCurrentRequest(controller, expectedDeviceId)) return
      pendingTurnRef.current = null
      setTurnInput('')
      setSessions((current) => current.map((item) => (
        item.id === session.id ? { ...item, state: 'running' } : item
      )))
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        if (pendingTurnRef.current?.requestId === requestId) {
          setError(errorMessage(reason, '无法发送 Runtime Turn'))
        }
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const interruptTurn = async () => {
    const session = selectedSession
    if (!session || !isRuntimeSessionForDevice(session, device.id)) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.interruptRuntimeTurn(session.id, { signal: controller.signal })
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法中断 Runtime Turn'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const stopSession = async () => {
    const session = selectedSession
    if (!session || !isRuntimeSessionForDevice(session, device.id)
      || !window.confirm(`停止 Runtime Session ${session.id.slice(0, 12)}？`)) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.stopRuntimeSession(session.id, { signal: controller.signal })
      if (!isCurrentRequest(controller, expectedDeviceId)) return
      setSessions((current) => current.map((item) => (
        item.id === session.id ? { ...item, state: 'stopping' } : item
      )))
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法停止 Runtime Session'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const respondDecision = async (decision: 'approve_once' | 'deny' | 'cancel_turn') => {
    const session = selectedSession
    const requestId = session?.active_request_id
    if (!session || !requestId || !isRuntimeSessionForDevice(session, device.id)) return
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.respondRuntimeInteraction(session.id, requestId, { decision }, { signal: controller.signal })
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法提交审批结果'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const respondAnswers = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    const session = selectedSession
    const requestId = session?.active_request_id
    if (!session || !requestId || !isRuntimeSessionForDevice(session, device.id)
      || questions.some((question) => !answers[question.id]?.trim())) return
    const submittedAnswers = Object.fromEntries(
      questions.map((question) => [question.id, answers[question.id].trim()]),
    )
    const expectedDeviceId = device.id
    const controller = beginRequest()
    setBusy(true); setError('')
    try {
      await api.respondRuntimeInteraction(
        session.id,
        requestId,
        { answers: submittedAnswers },
        { signal: controller.signal },
      )
      if (isCurrentRequest(controller, expectedDeviceId)) setAnswers({})
    } catch (reason) {
      if (isCurrentRequest(controller, expectedDeviceId) && !isAbortError(reason)) {
        setError(errorMessage(reason, '无法提交用户输入'))
      }
    } finally {
      const current = isCurrentRequest(controller, expectedDeviceId)
      endRequest(controller)
      if (current) setBusy(false)
    }
  }

  const copyToken = async () => {
    const value = enrollment
    const expectedDeviceId = device.id
    if (!value || value.device_id !== expectedDeviceId) return
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(value.enrollment_token)
      if (!mountedRef.current || deviceIdRef.current !== expectedDeviceId) return
      setCopied(true)
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current)
      copyTimerRef.current = window.setTimeout(() => {
        if (mountedRef.current) setCopied(false)
      }, 2500)
    } catch {
      if (mountedRef.current && deviceIdRef.current === expectedDeviceId) {
        setError('浏览器无法访问剪贴板，请手动复制一次性凭据。')
      }
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) closeDialog() }}>
      <DialogContent className="max-h-[min(820px,calc(100dvh-2rem))] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Cpu className="size-5 text-primary" />{device.name} · Agent Runtime</DialogTitle>
          <DialogDescription>
            每台设备只需安装并配对一次常驻 Host；Codex 通过 app-server 原生协议接入，不需要逐机配置 Hook。
          </DialogDescription>
        </DialogHeader>

        <section className="grid gap-3 rounded-lg border border-[#293641] bg-[#0b1117] p-4 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <strong className="text-[#dce6ed]">{runtimeLabel(device)}</strong>
              <p className="mt-1 mb-0 text-[#71808c]">Provider：{providerSummary}</p>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={busy || !registered} onClick={() => void probe()}><Radio />探测</Button>
              <Button variant="destructive" size="sm" disabled={busy || !registered} onClick={() => void revoke()}><ShieldOff />撤销</Button>
            </div>
          </div>
          {device.runtime && registered && (
            <div className="grid grid-cols-2 gap-2 font-mono text-[10px] text-[#71808c] max-sm:grid-cols-1">
              <span>Host {device.runtime.host_version || 'unknown'} · protocol {device.runtime.protocol_version}</span>
              <span className="truncate">instance {device.runtime.instance_id || 'unknown'}</span>
            </div>
          )}
        </section>

        {!registered && (
          <section className="grid gap-3 rounded-lg border border-[#293641] p-4">
            <div>
              <h3 className="m-0 text-sm text-[#dce6ed]">配对 Runtime Host</h3>
              <p className="mt-1 mb-0 text-xs text-[#71808c]">凭据只能使用一次，并会在短时间后过期。服务端只保存哈希。</p>
            </div>
            {!enrollment ? (
              <Button disabled={busy} onClick={() => void pair()}>{busy ? <LoaderCircle className="animate-spin" /> : <Cpu />}生成一次性配对凭据</Button>
            ) : (
              <div className="grid gap-3">
                <div className="flex gap-2">
                  <code aria-label="一次性配对凭据" className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-md border border-input bg-[#090d12] px-3 py-2 font-mono text-[10px]">
                    {enrollment.enrollment_token}
                  </code>
                  <Button variant="outline" size="icon" aria-label="复制配对凭据" disabled={busy} onClick={() => void copyToken()}><Copy /></Button>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-[#71808c]">
                  <span>有效期至 {new Date(enrollment.expires_at * 1000).toLocaleString()}</span>
                  <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void pair()}>重新生成</Button>
                </div>
                {copied && <p role="status" className="m-0 text-[10px] text-primary">已复制；使用后请用其他内容覆盖系统剪贴板。</p>}
                <pre className="m-0 overflow-x-auto whitespace-pre-wrap rounded-md bg-[#060a0e] p-3 font-mono text-[10px] leading-relaxed text-[#9eacb6]">{installCommand}</pre>
                <p className="m-0 text-[10px] text-[#71808c]">在目标 Linux 设备以持有 Codex 登录态的普通用户运行，不要使用 sudo；该账号会自动成为 Runtime 用户，脚本只在系统配置步骤按需提权。已有隧道时添加 <code>--runtime-only</code>。长期凭据不进入命令行或环境变量。</p>
              </div>
            )}
          </section>
        )}

        {registered && (
          <>
          <form onSubmit={startSession} className="grid gap-3 rounded-lg border border-[#293641] p-4">
            <div>
              <h3 className="m-0 text-sm text-[#dce6ed]">启动 Codex app-server 会话</h3>
              <p className="mt-1 mb-0 text-xs text-[#71808c]">会话固定在当前设备；跨设备迁移必须显式 checkpoint/resume。</p>
            </div>
            <Label>工作目录<Input required value={cwd} onChange={(event) => setCwd(event.target.value)} placeholder="/workspace/project" /></Label>
            <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
              <Label>模型（可选）<Input value={model} onChange={(event) => setModel(event.target.value)} placeholder="使用 Codex 默认值" /></Label>
              <Label>权限模式
                <select value={permissionMode} onChange={(event) => setPermissionMode(event.target.value as RuntimeSession['permission_mode'])} className="mt-2 h-9 w-full rounded-md border border-input bg-[#090d12] px-3 text-sm outline-none">
                  <option value="approval-required">只读并逐次审批</option>
                  <option value="workspace-write">工作区可写</option>
                  <option value="auto">自动审阅</option>
                  <option value="full-access">完全访问</option>
                </select>
              </Label>
            </div>
            <Button disabled={busy || !canStartCodex} title={!canStartCodex ? '等待 Runtime Host 上报可用的 Codex Provider' : undefined}>
              {busy ? <LoaderCircle className="animate-spin" /> : <Play />}创建会话
            </Button>
            {!canStartCodex && <p className="m-0 text-[10px] text-[#d8a45b]">当前 Host 未在线或 Codex Provider 不可用；仍可查看已有会话。</p>}
            {sessionsError && <p role="status" className="m-0 text-[10px] text-[#ffadb5]">{sessionsError}</p>}
            {sessions.length > 0 && (
              <div className="grid gap-2 border-t border-[#293641] pt-3">
                {sessions.slice(0, 6).map((session) => (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => setSelectedSessionId(session.id)}
                    className={`flex items-center justify-between rounded-md border px-3 py-2 text-left text-[10px] ${selectedSessionId === session.id ? 'border-primary/60 bg-primary/10' : 'border-transparent bg-[#0b1117]'}`}
                  >
                    <span className="truncate font-mono text-[#aebac3]">{session.id.slice(0, 12)} · {session.cwd}</span>
                    <span className="ml-3 text-[#71808c]">{session.state}</span>
                  </button>
                ))}
              </div>
            )}
          </form>
          {selectedSession && (
            <section className="grid gap-3 rounded-lg border border-[#293641] p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="m-0 truncate font-mono text-xs text-[#dce6ed]">Session {selectedSession.id}</h3>
                  <p className="mt-1 mb-0 truncate text-[10px] text-[#71808c]">{selectedSession.state} · {selectedSession.cwd}</p>
                </div>
                <div className="flex gap-2">
                  {['running', 'waiting'].includes(selectedSession.state) && (
                    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void interruptTurn()}><Square />中断</Button>
                  )}
                  {!['stopping', 'stopped', 'failed', 'lost'].includes(selectedSession.state) && (
                    <Button type="button" variant="destructive" size="sm" disabled={busy} onClick={() => void stopSession()}><ShieldOff />停止</Button>
                  )}
                </div>
              </div>

              {selectedSession.state === 'ready' && (
                <form onSubmit={startTurn} className="grid gap-2 border-t border-[#293641] pt-3">
                  <Label htmlFor="runtime-turn-input">发送 Turn</Label>
                  <textarea
                    id="runtime-turn-input"
                    required
                    rows={3}
                    value={turnInput}
                    onChange={(event) => setTurnInput(event.target.value)}
                    placeholder="描述要在这台设备上完成的任务"
                    className="w-full resize-y rounded-md border border-input bg-[#090d12] px-3 py-2 text-sm outline-none focus:border-primary"
                  />
                  <Button disabled={busy || !turnInput.trim()}>{busy ? <LoaderCircle className="animate-spin" /> : <Send />}发送到 {device.name}</Button>
                </form>
              )}

              {selectedSession.state === 'waiting' && selectedSession.active_request_id && (
                <div className="grid gap-3 border-t border-[#293641] pt-3">
                  <div>
                    <strong className="text-xs text-[#dce6ed]">等待交互</strong>
                    <p className="mt-1 mb-0 font-mono text-[10px] text-[#71808c]">{selectedSession.active_request_id}</p>
                  </div>
                  {questions.length > 0 ? (
                    <form onSubmit={respondAnswers} className="grid gap-3">
                      {questions.map((question) => (
                        <Label key={question.id}>{question.header || question.question || question.id}
                          {question.options?.length ? (
                            <select
                              required
                              value={answers[question.id] || ''}
                              onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
                              className="mt-2 h-9 w-full rounded-md border border-input bg-[#090d12] px-3 text-sm outline-none"
                            >
                              <option value="">请选择</option>
                              {question.options.map((option) => <option key={option.label} value={option.label}>{option.label}</option>)}
                            </select>
                          ) : (
                            <Input required value={answers[question.id] || ''} onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))} />
                          )}
                          {question.question && question.header && <small className="mt-1 block text-[10px] text-[#71808c]">{question.question}</small>}
                        </Label>
                      ))}
                      <Button disabled={busy || questions.some((question) => !answers[question.id]?.trim())}>提交输入</Button>
                    </form>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      <Button type="button" size="sm" disabled={busy} onClick={() => void respondDecision('approve_once')}>允许一次</Button>
                      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void respondDecision('deny')}>拒绝</Button>
                      <Button type="button" variant="destructive" size="sm" disabled={busy} onClick={() => void respondDecision('cancel_turn')}>取消 Turn</Button>
                    </div>
                  )}
                </div>
              )}

              {selectedSession.last_error && <p className="m-0 text-xs text-[#ffadb5]">{selectedSession.last_error}</p>}
              {eventsError && <p role="status" className="m-0 text-[10px] text-[#ffadb5]">{eventsError}</p>}
              <div className="grid max-h-48 gap-1 overflow-y-auto border-t border-[#293641] pt-3">
                <strong className="mb-1 text-[10px] tracking-wide text-[#71808c]">事件时间线</strong>
                {[...events].reverse().slice(0, 20).map((event) => (
                  <div key={event.event_id} className="grid grid-cols-[70px_1fr] gap-2 rounded bg-[#0b1117] px-2 py-1.5 text-[10px]">
                    <time className="font-mono text-[#596672]">{eventTime(event)}</time>
                    <span className="min-w-0 truncate font-mono text-[#aebac3]">{event.type}</span>
                  </div>
                ))}
                {!events.length && <span className="text-[10px] text-[#596672]">尚无 Runtime 事件</span>}
              </div>
            </section>
          )}
          </>
        )}

        {error && <p role="alert" className="m-0 rounded-md border border-[#713640] bg-[#28171b] px-3 py-2 text-xs text-[#ffadb5]">{error}</p>}
        <DialogFooter><Button variant="ghost" onClick={closeDialog}>关闭</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
