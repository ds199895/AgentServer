import { ChevronDown } from 'lucide-react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

export type MainPage = 'devices' | 'setup' | 'terminals'

type Props = {
  username: string
  page: MainPage
  deviceSummary: string
  sessionCount: number
  onNavigate: (page: MainPage) => void
  onShowPassword: () => void
  onLogout: () => void
}

function HeaderTab({ active, onClick, children, badge }: { active: boolean; onClick: () => void; children: React.ReactNode; badge?: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'relative flex cursor-pointer items-center gap-2 px-[17px] text-xs text-[#798692] transition-colors hover:bg-[#10171e] hover:text-[#c9d3db] max-md:gap-[5px] max-md:px-[clamp(7px,2.4vw,13px)] max-md:text-[10px]',
        active && 'bg-[#10171e] text-[#edf4f9]',
      )}
    >
      {children}
      {badge !== undefined && (
        <span
          className={cn(
            'min-w-[25px] rounded-[10px] border border-[#2b3742] bg-[#0b1016] px-1.5 py-[3px] text-center font-mono text-[8px] text-[#73818d] max-md:min-w-[20px] max-md:px-1',
            active && 'border-[#315a48] bg-[#12241d] text-primary',
          )}
        >
          {badge}
        </span>
      )}
      {active && (
        <span className="absolute inset-x-3 bottom-0 h-0.5 rounded-t-sm bg-primary shadow-[0_-3px_12px_#77f2b455]" />
      )}
    </button>
  )
}

export function Topbar({ username, page, deviceSummary, sessionCount, onNavigate, onShowPassword, onLogout }: Props) {
  return (
    <header className="z-[5] grid grid-cols-[1fr_auto_1fr] items-center border-b border-border bg-[#0b0f14] px-[18px] max-md:grid-cols-[auto_minmax(0,1fr)_auto] max-md:px-2">
      <button onClick={() => onNavigate('devices')} className="flex cursor-pointer items-center gap-[11px] justify-self-start font-bold tracking-[-0.02em] text-[#edf4f9]">
        <img src="/favicon.png" alt="" className="size-[31px] rounded-[7px] [image-rendering:pixelated]" />
        <span className="max-md:hidden">AgentServer</span>
      </button>
      <nav aria-label="主导航" className="flex h-full items-stretch gap-1 max-md:min-w-0 max-md:justify-self-center">
        <HeaderTab active={page === 'devices'} onClick={() => onNavigate('devices')} badge={deviceSummary}>设备列表</HeaderTab>
        <HeaderTab active={page === 'terminals'} onClick={() => onNavigate('terminals')} badge={sessionCount}>终端</HeaderTab>
        <HeaderTab active={page === 'setup'} onClick={() => onNavigate('setup')}>安装客户端</HeaderTab>
      </nav>
      <div className="justify-self-end">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex cursor-pointer items-center gap-[9px] rounded-lg px-2 py-[5px] text-xs transition-colors hover:bg-[#151c24]">
              <span className="grid size-[29px] place-items-center rounded-[7px] border border-[#314052] bg-[#17211d] font-mono text-[11px] font-medium text-primary">
                {username[0].toUpperCase()}
              </span>
              <span className="max-lg:hidden">{username}</span>
              <ChevronDown className="size-3.5 text-[#65717d] max-lg:hidden" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onShowPassword}>修改密码</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void onLogout()}>退出登录</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  )
}
