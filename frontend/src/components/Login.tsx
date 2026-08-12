import { FormEvent, useState } from 'react'

import { api } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export function Login({ onLogin }: { onLogin: (username: string) => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin((await api.login(username, password)).username)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="grid h-full w-full place-items-center bg-[#080b0f] bg-[radial-gradient(circle_at_18%_16%,rgba(34,86,65,.34),transparent_30%),radial-gradient(circle_at_82%_85%,rgba(34,62,86,.2),transparent_32%),linear-gradient(rgba(255,255,255,.025)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.025)_1px,transparent_1px)] p-6 [background-size:auto,auto,48px_48px,48px_48px]">
      <section className="w-full max-w-[470px] rounded-[18px] border border-[#293641] bg-[#0d1218ee] p-[42px] shadow-[0_28px_80px_#0009] backdrop-blur-[14px] max-md:px-6 max-md:py-[30px]">
        <img src="/favicon.png" alt="" aria-hidden="true" className="mb-[30px] size-[54px] rounded-xl shadow-[0_0_35px_#77f2b43b] [image-rendering:pixelated]" />
        <Eyebrow>AGENT NEXUS</Eyebrow>
        <h1 className="m-0 text-[clamp(27px,5vw,37px)] leading-[1.23] tracking-[-0.04em] text-[#f1f6fa]">
          所有设备，<br />一处安全连接。
        </h1>
        <p className="mt-[18px] mb-[30px] text-sm leading-[1.7] text-[#8996a3]">
          集中查看 FRP 隧道状态，并通过 SSH 打开设备终端。
        </p>
        <form onSubmit={submit} className="grid gap-[17px]">
          <Label>
            用户名
            <Input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" className="h-auto px-3.5 py-[13px]" />
          </Label>
          <Label>
            密码
            <Input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" autoFocus className="h-auto px-3.5 py-[13px]" />
          </Label>
          {error && <p className="-mt-1 text-xs text-[#ff8290]">{error}</p>}
          <Button disabled={busy} className="h-auto w-full px-4 py-[13px] font-bold">
            {busy ? '正在登录…' : '进入控制台'}
          </Button>
        </form>
      </section>
    </main>
  )
}
