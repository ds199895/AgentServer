import { useEffect, useMemo, useState } from 'react'
import { ExternalLink, LoaderCircle, MonitorPlay, Trash2 } from 'lucide-react'

import { api, type Device, type Preview, type TerminalSession } from '@/api'
import { Button } from '@/components/ui/button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

type Props = {
  devices: Device[]
  sessions: TerminalSession[]
  previews: Preview[]
  initialDeviceId?: string | null
  initialTerminalId?: string | null
  onClose: () => void
  onCreated: (preview: Preview, authorizedUrl: string) => void
  onDeleted: (id: string) => void
}

export function PreviewDialog({ devices, sessions, previews, initialDeviceId, initialTerminalId, onClose, onCreated, onDeleted }: Props) {
  const availableDevices = useMemo(() => devices.filter((device) => device.ssh_available), [devices])
  const [deviceId, setDeviceId] = useState(initialDeviceId || availableDevices[0]?.id || '')
  const [port, setPort] = useState('5173')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const relatedSession = useMemo(
    () => sessions.find((session) => session.id === initialTerminalId),
    [initialTerminalId, sessions],
  )

  useEffect(() => {
    if (initialDeviceId) setDeviceId(initialDeviceId)
  }, [initialDeviceId])

  const openExisting = async (preview: Preview) => {
    setBusy(true); setError('')
    try {
      const { url } = await api.previewTicket(preview.id)
      onCreated(preview, url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开预览')
    } finally { setBusy(false) }
  }

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    const numericPort = Number(port)
    if (!Number.isInteger(numericPort) || numericPort < 1 || numericPort > 65535) {
      setError('端口必须位于 1-65535')
      return
    }
    setBusy(true); setError('')
    try {
      const relatedTerminal = initialTerminalId && sessions.some((session) => session.id === initialTerminalId && session.device_id === deviceId)
        ? initialTerminalId
        : undefined
      const preview = await api.createPreview(deviceId, numericPort, label, relatedTerminal)
      const { url } = await api.previewTicket(preview.id)
      onCreated(preview, url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法创建预览')
    } finally { setBusy(false) }
  }

  const remove = async (preview: Preview) => {
    setBusy(true); setError('')
    try {
      await api.deletePreview(preview.id)
      onDeleted(preview.id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法关闭预览')
    } finally { setBusy(false) }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-h-[min(760px,calc(100dvh-2rem))] w-[calc(100%-2rem)] max-w-[620px] overflow-y-auto p-0 sm:max-w-[620px]">
        <div className="border-b border-[#26323c] px-6 pt-6 pb-5">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-lg"><MonitorPlay className="size-5 text-primary" />设备开发预览</DialogTitle>
            <DialogDescription>通过已有 SSH 通道访问设备本地的开发服务，支持 HMR 和 WebSocket。</DialogDescription>
          </DialogHeader>
        </div>
        <form onSubmit={create} className="grid gap-4 px-6">
          <div className="grid gap-2">
            <Label htmlFor="preview-device">设备</Label>
            <select id="preview-device" value={deviceId} onChange={(event) => setDeviceId(event.target.value)} className="h-9 rounded-md border border-input bg-[#090d12] px-3 text-sm outline-none focus:border-ring" required>
              {!availableDevices.length && <option value="">没有 SSH 可用的设备</option>}
              {availableDevices.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}
            </select>
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] gap-3 max-sm:grid-cols-1">
            <div className="grid gap-2">
              <Label htmlFor="preview-port">本地端口</Label>
              <Input id="preview-port" type="number" min={1} max={65535} inputMode="numeric" value={port} onChange={(event) => setPort(event.target.value)} placeholder="5173" required />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="preview-label">名称（可选）</Label>
              <Input id="preview-label" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Vite / Storybook / 管理后台" maxLength={80} />
            </div>
          </div>
          {relatedSession && relatedSession.services.length > 0 && (
            <div className="grid gap-2">
              <Label>终端检测到的服务</Label>
              <div className="flex flex-wrap gap-2">
                {relatedSession.services.map((service) => (
                  <button
                    key={service.port}
                    type="button"
                    disabled={service.status === 'offline'}
                    onClick={() => {
                      setPort(String(service.port))
                      if (!label) setLabel(service.label)
                    }}
                    className="rounded-md border border-[#2e3d47] bg-[#0c1319] px-2.5 py-1.5 text-left text-[9px] text-[#a9b6bf] hover:border-[#47705e] hover:text-primary disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    <strong className="mr-1.5 text-[#dbe5eb]">{service.label}</strong>
                    :{service.port} · {service.status === 'online' ? '运行中' : service.status === 'checking' ? '检查中' : '已停止'}
                  </button>
                ))}
              </div>
            </div>
          )}
          {error && <p role="alert" className="m-0 rounded-md border border-[#713640] bg-[#28171b] px-3 py-2 text-xs text-[#ffadb5]">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>取消</Button>
            <Button type="submit" disabled={busy || !deviceId}>{busy ? <LoaderCircle className="animate-spin" /> : <MonitorPlay />}创建并打开</Button>
          </DialogFooter>
        </form>
        {previews.length > 0 && (
          <section className="border-t border-[#26323c] px-6 py-5">
            <h3 className="mt-0 mb-3 text-xs font-semibold tracking-[0.08em] text-[#8795a1] uppercase">活跃预览 · {previews.length}</h3>
            <div className="grid gap-2">
              {previews.map((preview) => (
                <article key={preview.id} className="flex items-center gap-3 rounded-lg border border-[#26323c] bg-[#0c1218] p-3">
                  <span className="size-2 flex-none rounded-full bg-primary shadow-[0_0_9px_#77f2b477]" />
                  <div className="min-w-0 flex-1">
                    <strong className="block truncate text-xs text-[#dce6ed]">{preview.label}</strong>
                    <small className="font-mono text-[9px] text-[#667581]">{preview.device_name} · localhost:{preview.target_port}</small>
                  </div>
                  <Button type="button" variant="outline" size="icon-sm" title="打开预览" aria-label={`打开 ${preview.label}`} disabled={busy} onClick={() => void openExisting(preview)}><ExternalLink /></Button>
                  <Button type="button" variant="destructive" size="icon-sm" title="关闭隧道" aria-label={`关闭 ${preview.label}`} disabled={busy} onClick={() => void remove(preview)}><Trash2 /></Button>
                </article>
              ))}
            </div>
          </section>
        )}
      </DialogContent>
    </Dialog>
  )
}
