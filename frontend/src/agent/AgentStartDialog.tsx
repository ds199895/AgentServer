import { FormEvent, useMemo, useState } from 'react'

import type { Device } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DirectoryPicker } from './DirectoryPicker'

export type AgentStartOptions = {
  provider: string
  cwd: string
  permission_mode: string
  model: string | null
}

export function AgentStartDialog({
  device,
  onClose,
  onStart,
}: {
  device: Device
  onClose: () => void
  onStart: (options: AgentStartOptions) => Promise<void>
}) {
  const providers = useMemo(
    () => (device.runtime?.providers || []).filter((value) => value.available),
    [device],
  )
  const preferred = providers.find((value) => value.id === 'codex')?.id || providers[0]?.id || ''
  const [provider, setProvider] = useState(preferred)
  const [cwd, setCwd] = useState('')
  const [permissionMode, setPermissionMode] = useState('workspace-write')
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!provider || !cwd.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      await onStart({
        provider,
        cwd: cwd.trim(),
        permission_mode: permissionMode,
        model: model.trim() || null,
      })
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Agent 会话启动失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <Eyebrow>REMOTE AGENT</Eyebrow>
          <DialogTitle>在 {device.name} 启动 Agent</DialogTitle>
          <DialogDescription>
            会话由该设备上的 Runtime Host 和 provider 实际执行，不经过服务器本地进程。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-4">
          <Label>
            Provider
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger className="h-auto w-full px-3.5 py-[13px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {providers.map((value) => <SelectItem key={value.id} value={value.id}>{value.id}{value.version ? ` · ${value.version}` : ''}</SelectItem>)}
              </SelectContent>
            </Select>
          </Label>
          <Label className="grid gap-1.5">
            设备工作目录
            <DirectoryPicker deviceId={device.id} value={cwd} onChange={setCwd} />
          </Label>
          <Label>
            权限模式
            <Select value={permissionMode} onValueChange={setPermissionMode}>
              <SelectTrigger className="h-auto w-full px-3.5 py-[13px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="approval-required">每次审批</SelectItem>
                <SelectItem value="workspace-write">工作区写入</SelectItem>
                <SelectItem value="full-access">完全访问</SelectItem>
              </SelectContent>
            </Select>
          </Label>
          <Label>
            模型（可选）
            <Input value={model} onChange={(event) => setModel(event.target.value)} placeholder="使用 provider 默认模型" className="h-auto px-3.5 py-[13px]" />
          </Label>
          {error && <p className="text-xs text-[#ff8290]">{error}</p>}
          <Button disabled={busy || !provider || !cwd.trim()} className="h-auto w-full px-4 py-[13px] font-bold">
            {busy ? '正在连接设备…' : '启动远程 Agent'}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
