import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { Activity, Cpu, LayoutGrid, List, MonitorPlay, Pencil, Plus, RefreshCw, Terminal, Trash2 } from 'lucide-react'

import { api, type Device, type RuntimeSession, type TerminalSession } from '@/api'
import type { RuntimeRoomSession } from '@/pixel/scene'
import { DeviceIcon, StateBadge } from '@/components/device-bits'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

const DeviceWorld = lazy(() => import('@/DeviceWorld'))

function relativeTime(timestamp: number | null): string {
  if (!timestamp) return '从未'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

function runtimeStateLabel(device: Device): string {
  switch (device.runtime?.state) {
    case 'online': return '在线'
    case 'degraded': return '降级'
    case 'offline': return '离线'
    case 'revoked': return '已撤销'
    default: return '未配对'
  }
}

type Props = {
  devices: Device[]
  sessions: TerminalSession[]
  busyId: string | null
  onOpen: (device: Device) => void
  onEdit: (device: Device) => void
  onDelete: (device: Device) => void
  onProbe: (device: Device) => void
  onSync: () => void
  onAdd: () => void
  onSelectTerminal: (sessionId: string) => void
  onSelectRuntime: (sessionId: string, deviceId: string) => void
  onPreview: (device?: Device) => void
  onRuntime: (device: Device) => void
}

export function DeviceDashboard({ devices, sessions, busyId, onOpen, onEdit, onDelete, onProbe, onSync, onAdd, onSelectTerminal, onSelectRuntime, onPreview, onRuntime }: Props) {
  const [view, setView] = useState<'world' | 'list'>('world')
  const [runtimeSessions, setRuntimeSessions] = useState<RuntimeRoomSession[]>([])
  const sessionsByDevice = useMemo(() => {
    const result = new Map<string, TerminalSession[]>()
    for (const session of sessions) {
      if (!session.device_id) continue
      const current = result.get(session.device_id)
      if (current) current.push(session)
      else result.set(session.device_id, [session])
    }
    return result
  }, [sessions])
  const online = devices.filter((device) => device.frp_online).length
  const sshReady = devices.filter((device) => device.ssh_available).length
  const runtimeReady = devices.filter((device) => device.runtime?.state === 'online').length
  const metrics: Array<[string, number]> = [
    ['注册设备', devices.length],
    ['隧道在线', online],
    ['SSH 可用', sshReady],
    ['Runtime 在线', runtimeReady],
  ]
  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    const refresh = async () => {
      const values = await Promise.all(devices.map(async (device) => {
        try {
          const result = await api.runtimeSessions(device.id)
          return result.sessions as RuntimeSession[]
        } catch {
          return []
        }
      }))
      if (!disposed) setRuntimeSessions(values.flat())
      if (!disposed) timer = window.setTimeout(() => void refresh(), 5000)
    }
    void refresh()
    return () => {
      disposed = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [devices])
  return (
    <section className="flex min-h-full flex-col bg-[#090d12] bg-[radial-gradient(circle_at_90%_0,#15302655,transparent_30%)] px-[clamp(18px,4vw,58px)] pt-5 pb-[34px] max-md:px-3 max-md:pt-6 max-md:pb-[50px]">
      <div className="mb-3.5 flex items-center justify-end gap-6">
        <div className="flex gap-2">
          <div role="group" aria-label="设备展示方式" className="flex h-8 rounded-[9px] border border-[#293641] bg-[#0d141a] p-[3px]">
            {(['world', 'list'] as const).map((option) => (
              <button
                key={option}
                onClick={() => setView(option)}
                className={cn(
                  'flex min-w-[62px] flex-1 cursor-pointer items-center justify-center gap-1 rounded-md px-2.5 text-[10px] whitespace-nowrap text-[#6f7d89] transition-colors hover:text-[#b9c5ce] [&_svg]:size-3',
                  view === option && 'bg-[#17251f] text-primary shadow-[inset_0_0_0_1px_#315a48]',
                )}
              >
                {option === 'world' ? <><LayoutGrid />房间</> : <><List />列表</>}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" aria-label="同步 FRP 设备" className="text-[10px] max-md:size-7 max-md:px-0" onClick={onSync}>
            <RefreshCw />
            <span className="max-md:hidden">同步</span>
          </Button>
          <Button variant="outline" size="sm" aria-label="打开设备开发预览" className="text-[10px] max-md:size-7 max-md:px-0" onClick={() => onPreview()}>
            <MonitorPlay />
            <span className="max-md:hidden">预览</span>
          </Button>
          <Button size="sm" aria-label="添加设备" className="text-[10px] font-bold max-md:size-7 max-md:px-0" onClick={onAdd}>
            <Plus />
            <span className="max-md:hidden">设备</span>
          </Button>
        </div>
      </div>
      {view === 'list' && (
        <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {metrics.map(([label, value]) => (
            <article key={label} className="flex min-h-[104px] flex-col justify-between rounded-[10px] border border-[#202b35] bg-[#0e141b] px-[19px] py-[17px] shadow-[0_12px_30px_#0003] max-md:min-h-[88px]">
              <span className="text-[11px] tracking-[0.05em] text-[#6f7d89]">{label}</span>
              <strong className="font-mono text-[30px] font-medium text-[#e9f0f5]">{value}</strong>
            </article>
          ))}
        </div>
      )}
      {view === 'world' ? (
        <Suspense fallback={
          <div className="flex min-h-[520px] flex-1 items-center justify-center gap-2.5 rounded-[14px] border border-[#21303a] bg-[#071018] font-mono text-[10px] text-[#718590] max-md:min-h-[380px]">
            <span className="size-2 animate-world-pulse rounded-full bg-primary shadow-[0_0_12px_var(--color-primary)]" />
            正在生成像素基地…
          </div>
        }>
          <DeviceWorld devices={devices} sessions={sessions} runtimeSessions={runtimeSessions} busyId={busyId} onOpen={onOpen} onProbe={onProbe} onEdit={onEdit} onSelectTerminal={onSelectTerminal} onSelectRuntime={(sessionId) => {
            const runtime = runtimeSessions.find((item) => item.id === sessionId)
            if (runtime) onSelectRuntime(sessionId, runtime.device_id)
          }} />
        </Suspense>
      ) : (
        <div className="overflow-auto rounded-[11px] border border-[#202b35] bg-[#0d1319] shadow-[0_18px_45px_#0004]">
          <Table className="min-w-[640px] max-md:min-w-0">
            <TableHeader>
              <TableRow>
                <TableHead>设备</TableHead><TableHead>FRP</TableHead><TableHead>SSH</TableHead><TableHead>Runtime</TableHead>
                <TableHead className="max-md:hidden">入口</TableHead><TableHead className="max-md:hidden">最后在线</TableHead><TableHead className="max-lg:hidden">版本</TableHead><TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {devices.map((device) => {
                const deviceSessions = sessionsByDevice.get(device.id) || []
                const recentSession = [...deviceSessions].reverse().find((session) => session.active) || deviceSessions.at(-1)
                return (
                <TableRow key={device.id}>
                  <TableCell>
                    <div className="flex min-w-[190px] items-center gap-[11px] max-md:min-w-0">
                      <DeviceIcon ready={device.ssh_available} partial={device.frp_online} />
                      <div>
                        <strong className="mb-1 block text-xs text-[#dce5eb]">{device.name}</strong>
                        <small className="block font-mono text-[9px] text-[#596672]">
                          {device.hostname || device.id}{device.discovered ? ' · 自动发现' : ''}
                        </small>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <StateBadge online={device.frp_online} label={device.frp_online ? '在线' : '离线'} />
                  </TableCell>
                  <TableCell title={device.last_error}>
                    <StateBadge online={device.ssh_available} label={device.ssh_available ? '可用' : '不可用'} />
                  </TableCell>
                  <TableCell>
                    <StateBadge
                      online={device.runtime?.state === 'online'}
                      label={runtimeStateLabel(device)}
                    />
                  </TableCell>
                  <TableCell className="max-md:hidden">
                    <code className="block font-mono text-[10px] text-[#b5c1cb]">127.0.0.1:{device.remote_port}</code>
                    <small className="mt-1 block max-w-[180px] truncate font-mono text-[9px] text-[#596672]">{device.proxy_name}</small>
                  </TableCell>
                  <TableCell className="max-md:hidden">{relativeTime(device.last_seen_at)}</TableCell>
                  <TableCell className="max-lg:hidden" title={device.client_ip}>
                    {device.client_version || '—'}{device.wire_protocol ? ` · ${device.wire_protocol}` : ''}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-[5px]">
                      <Button
                        variant="outline"
                        size="xs"
                        aria-label={recentSession ? `继续 ${device.name} 最近的终端` : `新建 ${device.name} 终端`}
                        title={recentSession ? `继续最近终端（共 ${deviceSessions.length} 个）` : '新建终端'}
                        className="max-md:size-7 max-md:rounded-md max-md:px-0"
                        disabled={!recentSession && (busyId === device.id || !device.ssh_available)}
                        onClick={() => recentSession ? onSelectTerminal(recentSession.id) : onOpen(device)}
                      >
                        <Terminal />
                        <span className="max-md:hidden">{recentSession ? `继续·${deviceSessions.length}` : '新建'}</span>
                      </Button>
                      {recentSession && (
                        <Button
                          variant="outline"
                          size="xs"
                          aria-label={`在 ${device.name} 新建终端`}
                          title="新建一个独立终端"
                          className="max-md:size-7 max-md:rounded-md max-md:px-0"
                          disabled={busyId === device.id || !device.ssh_available}
                          onClick={() => onOpen(device)}
                        >
                          <Plus />
                          <span className="max-md:hidden">新建</span>
                        </Button>
                      )}
                      <Button variant="outline" size="xs" aria-label={`预览 ${device.name} 的开发服务`} className="max-md:size-7 max-md:rounded-md max-md:px-0" disabled={!device.ssh_available} onClick={() => onPreview(device)}>
                        <MonitorPlay />
                        <span className="max-md:hidden">预览</span>
                      </Button>
                      <Button variant="outline" size="xs" aria-label={`检测 ${device.name}`} className="max-md:size-7 max-md:rounded-md max-md:px-0" onClick={() => onProbe(device)}>
                        <Activity />
                        <span className="max-md:hidden">检测</span>
                      </Button>
                      <Button variant="outline" size="xs" aria-label={`管理 ${device.name} 的 Agent Runtime`} className="max-md:size-7 max-md:rounded-md max-md:px-0" onClick={() => onRuntime(device)}>
                        <Cpu />
                        <span className="max-md:hidden">Runtime</span>
                      </Button>
                      <Button variant="outline" size="xs" aria-label={`编辑 ${device.name}`} className="max-md:size-7 max-md:rounded-md max-md:px-0" onClick={() => onEdit(device)}>
                        <Pencil />
                        <span className="max-md:hidden">编辑</span>
                      </Button>
                      <Button variant="destructive" size="xs" aria-label={`删除 ${device.name}`} className="max-md:size-7 max-md:rounded-md max-md:px-0" onClick={() => onDelete(device)}>
                        <Trash2 />
                        <span className="max-md:hidden">删除</span>
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
                )
              })}
              {!devices.length && (
                <TableRow>
                  <TableCell colSpan={8}>
                    <div className="px-5 py-[60px] text-center text-[#596672]">尚未发现设备。部署 frpc 后同步，或手动注册设备。</div>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </section>
  )
}
