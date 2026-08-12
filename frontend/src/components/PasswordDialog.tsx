import { FormEvent, useState } from 'react'

import { api } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export function PasswordDialog({ onClose }: { onClose: () => void }) {
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [error, setError] = useState('')
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    try {
      await api.changePassword(currentPassword, newPassword)
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '修改失败')
    }
  }
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="sm:max-w-[410px]">
        <DialogHeader>
          <Eyebrow>SECURITY</Eyebrow>
          <DialogTitle>修改密码</DialogTitle>
          <DialogDescription>输入当前密码，并设置至少 8 位的新密码。</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-[17px]">
          <Label>
            当前密码
            <Input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoFocus className="h-auto px-3.5 py-[13px]" />
          </Label>
          <Label>
            新密码（至少 8 位）
            <Input type="password" minLength={8} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} className="h-auto px-3.5 py-[13px]" />
          </Label>
          {error && <p className="-mt-1 text-xs text-[#ff8290]">{error}</p>}
          <Button className="h-auto w-full px-4 py-[13px] font-bold">保存新密码</Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
