import { useEffect, useRef, useState } from 'react'
import { AppWindow, Check, Copy, Download, KeyRound, LoaderCircle, Pencil, Terminal } from 'lucide-react'

import { api, type Device, type DeviceRuntimeEnrollment } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { bootstrapCurlProtocolArgs, prepareDeviceEnrollment } from '@/device-bootstrap'
import { cn, copyToClipboard } from '@/lib/utils'

function nextRemotePort(devices: Device[]): number {
  const usedPorts = devices
    .map((device) => device.remote_port)
    .filter((port) => port >= 20000 && port <= 29999)
  if (!usedPorts.length) return 20001
  const highestPort = Math.max(...usedPorts)
  if (highestPort < 29999) return highestPort + 1
  const used = new Set(usedPorts)
  for (let port = 20000; port <= 29999; port += 1) {
    if (!used.has(port)) return port
  }
  return 29999
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `\"'\"'`)}'`
}

function powershellQuote(value: string): string {
  return `'${value.replaceAll("'", "''")}'`
}

const INSTALL_STEPS: Array<[string, string]> = [
  ['确认端口唯一', '建议依次使用 20001、20002……不要在云安全组公开这些 SSH 转发端口。'],
  ['选择正确身份', '完整 Runtime bootstrap 由持有 Codex 登录态的 Linux 普通用户运行；传统 SSH 安装器才直接使用 sudo。'],
  ['输入 FRP token', '全新安装时会隐藏输入；合并现有配置时直接保留原 token，不会再次询问。'],
  ['已有 frpc 使用合并模式', '传入 --merge-existing 配置路径，脚本会备份、追加 SSH proxy、校验并重启原服务，不创建第二个 frpc。'],
  ['等待设备上线', '约 15 秒后返回“设备列表”，点击“同步 FRP”。新设备会自动出现。'],
  ['核对 SSH 用户', 'SSH 显示可用后即可打开终端；若用户名不正确，在设备列表中编辑。'],
]

function CommandBox({ command, copyLabel, dashed = false }: { command: string; copyLabel: string; dashed?: boolean }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = async () => {
    const copied = await copyToClipboard(command)
    setState(copied ? 'copied' : 'failed')
    window.setTimeout(() => setState('idle'), 1500)
  }
  return (
    <div className={cn(
      'col-span-full flex min-w-0 items-center gap-2 rounded-[7px] border border-[#25323d] bg-[#090e13] py-2.5 pr-2.5 pl-3',
      dashed && 'border-dashed bg-[#0d1419]',
    )}>
      <code className="min-w-0 flex-1 truncate font-mono text-[9px] text-[#9eacb8]">{command}</code>
      <button
        onClick={() => void copy()}
        className={cn(
          'flex flex-none cursor-pointer items-center gap-1 rounded-[5px] border border-[#2d3b46] bg-[#151d25] px-2 py-[5px] text-[9px] text-[#8795a0] transition-colors hover:border-[#416452] hover:text-primary',
          state === 'copied' && 'border-[#416452] text-primary',
          state === 'failed' && 'border-[#713640] text-[#ff8d98]',
        )}
      >
        {state === 'copied' ? <Check className="size-2.5" /> : <Copy className="size-2.5" />}
        {state === 'copied' ? '已复制' : state === 'failed' ? '复制失败' : copyLabel}
      </button>
    </div>
  )
}

function SecretBox({ value, copyLabel }: { value: string; copyLabel: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = async () => {
    const copied = await copyToClipboard(value)
    setState(copied ? 'copied' : 'failed')
    window.setTimeout(() => setState('idle'), 1500)
  }
  return (
    <div className="col-span-full flex min-w-0 items-center gap-2 rounded-[7px] border border-[#6b5830] bg-[#18150d] py-2.5 pr-2.5 pl-3">
      <code aria-label="一次性配对凭据" className="min-w-0 flex-1 truncate font-mono text-[9px] text-[#e6ca87]">{value}</code>
      <button
        type="button"
        onClick={() => void copy()}
        className={cn(
          'flex flex-none cursor-pointer items-center gap-1 rounded-[5px] border border-[#6b5830] bg-[#292212] px-2 py-[5px] text-[9px] text-[#d0b46d] transition-colors hover:border-[#9d8445] hover:text-[#f4d998]',
          state === 'copied' && 'border-[#416452] text-primary',
          state === 'failed' && 'border-[#713640] text-[#ff8d98]',
        )}
      >
        {state === 'copied' ? <Check className="size-2.5" /> : <Copy className="size-2.5" />}
        {state === 'copied' ? '已复制' : state === 'failed' ? '复制失败' : copyLabel}
      </button>
    </div>
  )
}

export function DownloadsPage({ devices, onChanged }: { devices: Device[]; onChanged: () => void }) {
  const [deviceId, setDeviceId] = useState('device-001')
  const [remotePort, setRemotePort] = useState(() => nextRemotePort(devices))
  const [portDraft, setPortDraft] = useState(() => String(nextRemotePort(devices)))
  const [editingPort, setEditingPort] = useState(false)
  const [portCustomized, setPortCustomized] = useState(false)
  const [sshUser, setSshUser] = useState('root')
  const [preparedDevice, setPreparedDevice] = useState<Device | null>(null)
  const [enrollment, setEnrollment] = useState<DeviceRuntimeEnrollment | null>(null)
  const [registrationBusy, setRegistrationBusy] = useState(false)
  const [registrationError, setRegistrationError] = useState('')
  const registrationGenerationRef = useRef(0)
  const parsedPort = Number(portDraft)
  const portError = !/^\d+$/.test(portDraft)
    ? '请输入数字端口'
    : parsedPort < 20000 || parsedPort > 29999
      ? '端口必须位于 20000–29999'
      : devices.some((device) => (
        device.remote_port === parsedPort && device.id !== deviceId.trim()
      ))
        ? `端口 ${parsedPort} 已被设备占用`
        : ''
  const clearPreparedEnrollment = () => {
    registrationGenerationRef.current += 1
    setPreparedDevice(null)
    setEnrollment(null)
    setRegistrationError('')
  }
  const beginPortEdit = () => {
    clearPreparedEnrollment()
    setPortCustomized(true)
    setPortDraft(String(remotePort))
    setEditingPort(true)
  }
  const confirmPort = () => {
    if (portError) return
    setRemotePort(parsedPort)
    setEditingPort(false)
  }
  useEffect(() => {
    if (portCustomized) return
    const nextPort = nextRemotePort(devices)
    setRemotePort(nextPort)
    setPortDraft(String(nextPort))
  }, [devices, portCustomized])
  const shellCommand = `sudo sh install-frpc-ssh.sh --device-id ${shellQuote(deviceId || 'device-001')} --remote-port ${remotePort} --ssh-user ${shellQuote(sshUser || 'root')}`
  const bootstrapCommand = preparedDevice
    ? `curl --fail --silent --show-error ${bootstrapCurlProtocolArgs(window.location.origin)} -o install-agentserver-device.sh ${shellQuote(`${window.location.origin}/device-bootstrap/install.sh`)} && bash install-agentserver-device.sh --device-id ${shellQuote(preparedDevice.id)} --base-url ${shellQuote(window.location.origin)} --remote-port ${preparedDevice.remote_port} --ssh-user ${shellQuote(preparedDevice.ssh_user)} --runtime-bundle-url ${shellQuote(window.location.origin)}`
    : ''
  const mergeCommand = `${shellCommand} --merge-existing /path/to/frpc.toml`
  const powershellCommand = `.\\install-frpc-ssh.ps1 -DeviceId ${powershellQuote(deviceId || 'device-001')} -RemotePort ${remotePort} -SshUser ${powershellQuote(sshUser || 'Administrator')}`
  const fieldClass = 'h-auto px-3.5 py-[13px]'
  const registerAndEnroll = async () => {
    const generation = registrationGenerationRef.current + 1
    registrationGenerationRef.current = generation
    setRegistrationBusy(true)
    setRegistrationError('')
    setPreparedDevice(null)
    setEnrollment(null)
    setPortCustomized(true)
    try {
      const prepared = await prepareDeviceEnrollment(api, devices, {
        deviceId,
        remotePort,
        sshUser,
      })
      if (registrationGenerationRef.current !== generation) return
      setPreparedDevice(prepared.device as Device)
      setEnrollment(prepared.enrollment)
      onChanged()
    } catch (reason) {
      if (registrationGenerationRef.current !== generation) return
      setRegistrationError(reason instanceof Error ? reason.message : '无法注册设备或生成配对凭据')
      onChanged()
    } finally {
      if (registrationGenerationRef.current === generation) setRegistrationBusy(false)
    }
  }
  useEffect(() => () => { registrationGenerationRef.current += 1 }, [])
  return (
    <section className="min-h-full bg-[#090d12] bg-[radial-gradient(circle_at_8%_0,#17392c66,transparent_28%),radial-gradient(circle_at_95%_40%,#17304744,transparent_30%)] px-[clamp(18px,5vw,72px)] pt-[38px] pb-[70px] max-md:px-3 max-md:pt-6 max-md:pb-[50px]">
      <div className="mb-[30px] flex items-start justify-between gap-6 max-md:flex-col max-md:items-stretch">
        <div>
          <Eyebrow>CLIENT INSTALLER</Eyebrow>
          <h1 className="m-0 text-[clamp(28px,4vw,42px)] tracking-[-0.045em] text-[#f0f5f8]">接入设备 Runtime 与 SSH 穿透</h1>
          <p className="mt-3 mb-0 max-w-[680px] text-[13px] leading-[1.7] text-[#7d8b97]">
            注册设备并生成一次性凭据；Linux 一键脚本会安装 FRP、配置 SSH，并启动常驻 Runtime Host。
          </p>
        </div>
        <span className="flex-none rounded-full border border-[#315a48] bg-[#11251d] px-2.5 py-[7px] font-mono text-[9px] text-[#7bd3a8] max-md:self-start">
          frp v0.69.0 · SHA-256 verified
        </span>
      </div>
      <div className="mb-4 grid grid-cols-[minmax(220px,.65fr)_1.35fr] items-center gap-7 rounded-[11px] border border-[#22303a] bg-[#0e151cdd] p-5 max-lg:grid-cols-1">
        <div>
          <h2 className="m-0 text-[17px] text-[#e4ebf0]">1. 设置设备参数</h2>
          <p className="mt-[7px] mb-0 text-[11px] text-[#697783]">设备 ID 和远端端口必须在所有设备之间唯一。</p>
        </div>
        <div className="grid grid-cols-[1.2fr_.8fr_1fr] items-start gap-2.5 max-lg:grid-cols-1">
          <Label>
            设备 ID
            <Input
              value={deviceId}
              disabled={registrationBusy}
              onChange={(event) => { clearPreparedEnrollment(); setDeviceId(event.target.value) }}
              className={fieldClass}
            />
          </Label>
          <Label className="content-start">
            远端端口
            {editingPort ? (
              <>
                <div className={cn(
                  'flex min-h-[42px] min-w-0 items-stretch overflow-hidden rounded-lg border border-input bg-[#090d12] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/20',
                  portError && 'border-[#a74752] focus-within:border-[#a74752] focus-within:ring-[#ff6b7a12]',
                )}>
                  <input
                    type="number"
                    min={20000}
                    max={29999}
                    value={portDraft}
                    disabled={registrationBusy}
                    onChange={(event) => { clearPreparedEnrollment(); setPortDraft(event.target.value) }}
                    onFocus={(event) => event.currentTarget.select()}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        confirmPort()
                      } else if (event.key === 'Escape') {
                        setPortDraft(String(remotePort))
                        setEditingPort(false)
                      }
                    }}
                    aria-invalid={Boolean(portError)}
                    autoFocus
                    className="min-w-0 flex-1 border-0 bg-transparent px-3.5 py-0 text-sm text-[#edf4f9] shadow-none outline-none"
                  />
                  <button
                    type="button"
                    disabled={Boolean(portError)}
                    onClick={confirmPort}
                    aria-label="确认远端端口"
                    className="grid w-10 flex-none cursor-pointer place-items-center border-l border-input bg-[#151d25] text-[#8c9aa6] transition-colors hover:bg-[#18251f] hover:text-primary disabled:cursor-not-allowed disabled:text-[#5d6872] disabled:hover:bg-[#151d25]"
                  >
                    <Check className="size-3.5" />
                  </button>
                </div>
                <small aria-live="polite" className={cn('min-h-3 text-[9px] leading-[1.35] font-normal', portError ? 'text-[#ff8290]' : 'text-[#65c997]')}>
                  {portError || '端口可用，按 Enter 确认'}
                </small>
              </>
            ) : (
              <div className="flex min-h-[42px] min-w-0 items-stretch overflow-hidden rounded-lg border border-input bg-[#090d12]">
                <code className="flex min-w-0 flex-1 items-center px-3.5 font-mono text-xs text-[#edf4f9]">{remotePort}</code>
                <button
                  type="button"
                  onClick={beginPortEdit}
                  aria-label="编辑远端端口"
                  title="编辑远端端口"
                  className="grid w-10 flex-none cursor-pointer place-items-center border-l border-input bg-[#151d25] text-[#8c9aa6] transition-colors hover:bg-[#18251f] hover:text-primary"
                >
                  <Pencil className="size-3" />
                </button>
              </div>
            )}
          </Label>
          <Label>
            SSH 用户
            <Input
              value={sshUser}
              disabled={registrationBusy}
              onChange={(event) => { clearPreparedEnrollment(); setSshUser(event.target.value) }}
              className={fieldClass}
            />
          </Label>
          <Button
            type="button"
            disabled={registrationBusy || editingPort || Boolean(portError)}
            onClick={() => void registerAndEnroll()}
            className="col-span-full"
          >
            {registrationBusy ? <LoaderCircle className="animate-spin" /> : <KeyRound />}
            {enrollment ? '重新生成一次性配对凭据' : '注册设备并生成配对凭据'}
          </Button>
          {registrationError && <p role="alert" className="col-span-full m-0 text-[10px] text-[#ff8290]">{registrationError}</p>}
          {enrollment && preparedDevice && (
            <p role="status" className="col-span-full m-0 text-[10px] text-[#65c997]">
              已注册 {preparedDevice.id}，配对凭据有效期至 {new Date(enrollment.expires_at * 1000).toLocaleString()}
            </p>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <article className="grid grid-cols-[48px_minmax(0,1fr)] content-start gap-4 rounded-xl border border-[#22303a] bg-[#0e151c] p-6 shadow-[0_18px_45px_#0004]">
          <div className="grid size-12 place-items-center rounded-[10px] border border-[#315a48] bg-[#14241e] text-primary"><Terminal className="size-5" /></div>
         <div>
           <Eyebrow>LINUX RUNTIME / LINUX + MACOS SSH</Eyebrow>
           <h2 className="mt-0 mb-2 text-lg text-[#e8eff4]">完整 Runtime 与 SSH 安装器</h2>
           <p className="m-0 text-[11px] leading-[1.65] text-[#71808c]">完整 Runtime bootstrap 目前仅支持 Linux；传统 SSH/FRP 脚本继续支持 Linux 与 macOS。</p>
         </div>
          <a
            href="/device-bootstrap/install.sh"
            download="install-agentserver-device.sh"
            className="col-span-full flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[#3b8062] bg-primary px-3.5 text-xs font-bold text-[#0a1510] no-underline transition-[transform,box-shadow] hover:-translate-y-px hover:shadow-[0_8px_25px_#77f2b42a]"
          >
            <Download className="size-3.5" />
            下载完整一键安装器
          </a>
          {enrollment && preparedDevice ? (
            <>
              <SecretBox value={enrollment.enrollment_token} copyLabel="复制配对凭据" />
              <CommandBox command={bootstrapCommand} copyLabel="复制一键命令" />
            </>
          ) : (
            <p className="col-span-full m-0 text-[10px] leading-[1.5] text-[#d8a45b]">先在上方注册设备并生成一次性配对凭据，随后才会显示绑定该设备的一键命令。</p>
          )}
         <a
           href="/downloads/install-frpc-ssh.sh"
           download
           className="col-span-full flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[#3b8062] bg-primary px-3.5 text-xs font-bold text-[#0a1510] no-underline transition-[transform,box-shadow] hover:-translate-y-px hover:shadow-[0_8px_25px_#77f2b42a]"
         >
           <Download className="size-3.5" />
           下载 .sh
         </a>
         <CommandBox command={shellCommand} copyLabel="复制" />
         <CommandBox command={mergeCommand} copyLabel="合并命令" dashed />
          <p className="col-span-full m-0 text-[10px] leading-[1.5] text-[#687682]">以 Codex 登录态普通用户执行完整命令，在隐藏提示中依次粘贴 FRP token 和上方配对凭据。传统脚本只安装 SSH 穿透。</p>
        </article>
        <article className="grid grid-cols-[48px_minmax(0,1fr)] content-start gap-4 rounded-xl border border-[#22303a] bg-[#0e151c] p-6 shadow-[0_18px_45px_#0004]">
          <div className="grid size-12 place-items-center rounded-[10px] border border-[#31536b] bg-[#12202b] text-[#78bfff]"><AppWindow className="size-5" /></div>
          <div>
            <Eyebrow>WINDOWS 10 / 11</Eyebrow>
            <h2 className="mt-0 mb-2 text-lg text-[#e8eff4]">PowerShell 自动安装器</h2>
            <p className="m-0 text-[11px] leading-[1.65] text-[#71808c]">支持 AMD64 和 ARM64，自动安装并启用 OpenSSH Server，注册 Windows 服务。</p>
          </div>
          <a
            href="/downloads/install-frpc-ssh.ps1"
            download
            className="col-span-full flex min-h-10 items-center justify-center gap-2 rounded-lg border border-[#3b8062] bg-primary px-3.5 text-xs font-bold text-[#0a1510] no-underline transition-[transform,box-shadow] hover:-translate-y-px hover:shadow-[0_8px_25px_#77f2b42a]"
          >
            <Download className="size-3.5" />
            下载 .ps1
          </a>
          <CommandBox command={powershellCommand} copyLabel="复制" />
        </article>
      </div>
      <div className="mt-4 grid grid-cols-[minmax(190px,.55fr)_1.45fr] gap-7 rounded-xl border border-[#22303a] bg-[#0d141a] p-6 max-lg:grid-cols-1">
        <div>
          <Eyebrow>QUICK START</Eyebrow>
          <h2 className="m-0 text-[17px] text-[#e4ebf0]">2. 运行与验证</h2>
        </div>
        <ol className="m-0 grid list-none gap-3.5 p-0">
          {INSTALL_STEPS.map(([title, detail], index) => (
            <li key={title} className="relative grid gap-[3px] pl-[34px]">
              <span className="absolute top-0 left-0 grid size-[23px] place-items-center rounded-md border border-[#315a48] bg-[#14241e] font-mono text-[9px] text-primary">
                {index + 1}
              </span>
              <strong className="text-[11px] text-[#cbd6de]">{title}</strong>
              <span className="text-[10px] leading-[1.55] text-[#687682]">{detail}</span>
            </li>
          ))}
        </ol>
      </div>
      <div className="mt-3.5 flex items-center gap-4 text-[10px] text-[#596773] max-md:flex-col max-md:items-stretch">
        <a href="/downloads/frpc.example.toml" download className="text-[#83c9a6] no-underline hover:underline">下载手动配置模板</a>
        <a href="/downloads/agentserver-ssh-key.pub" download className="text-[#83c9a6] no-underline hover:underline">下载 AgentServer SSH 公钥</a>
        <p className="m-0 ml-auto max-md:ml-0">
          仅传统 SSH Shell 命令支持 <code className="font-mono">--dry-run</code>；完整 Runtime bootstrap 不支持预演。正式使用前请配置 HTTPS。
        </p>
      </div>
    </section>
  )
}
