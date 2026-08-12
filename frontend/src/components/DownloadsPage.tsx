import { useEffect, useState } from 'react'
import { AppWindow, Check, Copy, Download, Pencil, Terminal } from 'lucide-react'

import type { Device } from '@/api'
import { Eyebrow } from '@/components/Eyebrow'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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

const INSTALL_STEPS: Array<[string, string]> = [
  ['确认端口唯一', '建议依次使用 20001、20002……不要在云安全组公开这些 SSH 转发端口。'],
  ['以管理员身份运行', 'Linux/macOS 使用 sudo；Windows 使用“以管理员身份运行”的 PowerShell。'],
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

export function DownloadsPage({ devices }: { devices: Device[] }) {
  const [deviceId, setDeviceId] = useState('device-001')
  const [remotePort, setRemotePort] = useState(() => nextRemotePort(devices))
  const [portDraft, setPortDraft] = useState(() => String(nextRemotePort(devices)))
  const [editingPort, setEditingPort] = useState(false)
  const [portCustomized, setPortCustomized] = useState(false)
  const [sshUser, setSshUser] = useState('root')
  const parsedPort = Number(portDraft)
  const portError = !/^\d+$/.test(portDraft)
    ? '请输入数字端口'
    : parsedPort < 20000 || parsedPort > 29999
      ? '端口必须位于 20000–29999'
      : devices.some((device) => device.remote_port === parsedPort)
        ? `端口 ${parsedPort} 已被设备占用`
        : ''
  const beginPortEdit = () => {
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
  const shellCommand = `sudo sh install-frpc-ssh.sh --device-id ${deviceId || 'device-001'} --remote-port ${remotePort} --ssh-user ${sshUser || 'root'}`
  const mergeCommand = `${shellCommand} --merge-existing /path/to/frpc.toml`
  const powershellCommand = `.\\install-frpc-ssh.ps1 -DeviceId ${deviceId || 'device-001'} -RemotePort ${remotePort} -SshUser ${sshUser || 'Administrator'}`
  const fieldClass = 'h-auto px-3.5 py-[13px]'
  return (
    <section className="min-h-full bg-[#090d12] bg-[radial-gradient(circle_at_8%_0,#17392c66,transparent_28%),radial-gradient(circle_at_95%_40%,#17304744,transparent_30%)] px-[clamp(18px,5vw,72px)] pt-[38px] pb-[70px] max-md:px-3 max-md:pt-6 max-md:pb-[50px]">
      <div className="mb-[30px] flex items-start justify-between gap-6 max-md:flex-col max-md:items-stretch">
        <div>
          <Eyebrow>CLIENT INSTALLER</Eyebrow>
          <h1 className="m-0 text-[clamp(28px,4vw,42px)] tracking-[-0.045em] text-[#f0f5f8]">安装设备端 SSH 穿透</h1>
          <p className="mt-3 mb-0 max-w-[680px] text-[13px] leading-[1.7] text-[#7d8b97]">
            选择设备参数，下载对应系统的安装器。脚本会安装 frpc、配置 SSH 公钥并注册开机服务。
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
            <Input value={deviceId} onChange={(event) => setDeviceId(event.target.value)} className={fieldClass} />
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
                    onChange={(event) => setPortDraft(event.target.value)}
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
            <Input value={sshUser} onChange={(event) => setSshUser(event.target.value)} className={fieldClass} />
          </Label>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        <article className="grid grid-cols-[48px_minmax(0,1fr)] content-start gap-4 rounded-xl border border-[#22303a] bg-[#0e151c] p-6 shadow-[0_18px_45px_#0004]">
          <div className="grid size-12 place-items-center rounded-[10px] border border-[#315a48] bg-[#14241e] text-primary"><Terminal className="size-5" /></div>
          <div>
            <Eyebrow>LINUX / MACOS</Eyebrow>
            <h2 className="mt-0 mb-2 text-lg text-[#e8eff4]">Shell 自动安装器</h2>
            <p className="m-0 text-[11px] leading-[1.65] text-[#71808c]">支持 x86_64、ARM64、ARMv7、RISC-V 和 LoongArch。自动配置 systemd 或 launchd。</p>
          </div>
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
          可先在 Shell 命令末尾添加 <code className="font-mono">--dry-run</code> 无修改验证；正式使用前请配置 HTTPS。
        </p>
      </div>
    </section>
  )
}
