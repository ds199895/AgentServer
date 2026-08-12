import { FormEvent, useState } from 'react'

import { api, type Device, type DeviceInput } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

export function DeviceDialog({ device, onClose, onSaved }: { device?: Device; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<DeviceInput>({
    id: device?.id || '', name: device?.name || '', proxy_name: device?.proxy_name || '',
    remote_port: device?.remote_port || 20000, ssh_user: device?.ssh_user || 'root',
    remote_shell: device?.remote_shell || 'system', notes: device?.notes || '',
  })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (key: keyof DeviceInput, value: string | number) => setForm((current) => ({ ...current, [key]: value }))
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (device) await api.updateDevice(device.id, form)
      else await api.createDevice(form)
      onSaved()
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }
  const inputClass = 'h-auto px-3.5 py-[13px]'
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <Eyebrow>DEVICE</Eyebrow>
          <DialogTitle>{device ? '编辑设备' : '注册设备'}</DialogTitle>
          <DialogDescription>
            {device ? '更新设备的 FRP、SSH 和默认终端参数。' : '填写设备标识和 SSH 穿透参数，将设备加入控制台。'}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-[17px]">
          {!device && (
            <Label>
              设备 ID
              <Input required minLength={2} value={form.id} onChange={(event) => set('id', event.target.value)} placeholder="device-001" className={inputClass} />
            </Label>
          )}
          <Label>
            显示名称
            <Input required value={form.name} onChange={(event) => set('name', event.target.value)} placeholder="开发机 01" className={inputClass} />
          </Label>
          <Label>
            FRP 代理名称
            <Input required value={form.proxy_name} onChange={(event) => set('proxy_name', event.target.value)} placeholder="device-001.ssh" className={inputClass} />
          </Label>
          <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
            <Label>
              远端端口
              <Input required type="number" min={1} max={65535} value={form.remote_port} onChange={(event) => set('remote_port', Number(event.target.value))} className={inputClass} />
            </Label>
            <Label>
              SSH 用户
              <Input required value={form.ssh_user} onChange={(event) => set('ssh_user', event.target.value)} className={inputClass} />
            </Label>
          </div>
          <Label>
            默认远程 Shell
            <Select value={form.remote_shell} onValueChange={(value) => set('remote_shell', value)}>
              <SelectTrigger className="h-auto w-full px-3.5 py-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="system">系统默认</SelectItem>
                <SelectItem value="powershell">PowerShell</SelectItem>
                <SelectItem value="cmd">CMD</SelectItem>
              </SelectContent>
            </Select>
          </Label>
          <Label>
            备注
            <Input value={form.notes} onChange={(event) => set('notes', event.target.value)} className={inputClass} />
          </Label>
          {error && <p className="-mt-1 text-xs text-[#ff8290]">{error}</p>}
          <Button disabled={busy} className="h-auto w-full px-4 py-[13px] font-bold">
            {busy ? '保存中…' : '保存设备'}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
