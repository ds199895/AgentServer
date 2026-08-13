import { useEffect, useRef } from 'react'
import { LoaderCircle, MonitorPlay, Plus, XIcon } from 'lucide-react'

import type { TerminalSession } from '@/api'
import { getCharacter, hashString, type CharPose } from '@/pixel/sprites'
import { cn } from '@/lib/utils'

/**
 * The pixel character that represents this session in the device world,
 * redrawn tiny for the tab bar. Sprites are cached by getCharacter, so each
 * tab only pays for one drawImage whenever identity/pose/active changes.
 */
function SessionSprite({ session, pose }: { session: TerminalSession; pose: CharPose }) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    const context = canvas?.getContext('2d')
    if (!canvas || !context) return
    // Same variant rule as buildScene: the tab shows the same person.
    const sprite = getCharacter(pose, hashString(session.id), 0, false, session.active)
    const scale = Math.min(canvas.width / sprite.width, canvas.height / sprite.height)
    const width = Math.floor(sprite.width * scale)
    const height = Math.floor(sprite.height * scale)
    context.imageSmoothingEnabled = false
    context.clearRect(0, 0, canvas.width, canvas.height)
    context.drawImage(sprite, Math.floor((canvas.width - width) / 2), canvas.height - height, width, height)
  }, [session.id, session.active, pose])
  return <canvas ref={ref} width={18} height={24} aria-hidden="true" className="my-auto flex-none" />
}

export type TerminalGroup = {
  key: string
  deviceId: string | null
  name: string
  sessions: TerminalSession[]
}

export function groupTerminalSessions(sessions: TerminalSession[]): TerminalGroup[] {
  const groups = new Map<string, TerminalGroup>()
  sessions.forEach((session) => {
    // Local terminals do not belong to a device, so each remains independent.
    const key = session.device_id ? `device:${session.device_id}` : `session:${session.id}`
    const existing = groups.get(key)
    if (existing) {
      existing.sessions.push(session)
      return
    }
    groups.set(key, {
      key,
      deviceId: session.device_id,
      name: session.device_name || session.name,
      sessions: [session],
    })
  })
  return [...groups.values()]
}

type Props = {
  sessions: TerminalSession[]
  activeId: string | null
  cloningId: string | null
  onSelect: (id: string) => void
  onClose: (session: TerminalSession) => void
  onClone: (source: TerminalSession) => void
  onPreview: (source: TerminalSession) => void
}

export function TerminalTabsBar({ sessions, activeId, cloningId, onSelect, onClose, onClone, onPreview }: Props) {
  const terminalGroups = groupTerminalSessions(sessions)
  return (
    <nav aria-label="按设备分组的终端列表" className="flex min-w-0 items-center gap-1.5 overflow-x-auto border-b border-border bg-[#0b1016] px-2.5 py-[5px] [scrollbar-color:#34404c_transparent] [scrollbar-width:thin] max-md:px-1.5">
      {terminalGroups.map((group) => {
        const cloneSource = group.sessions.find((session) => session.id === activeId) || group.sessions[0]
        const groupActive = group.sessions.some((session) => session.id === activeId)
        const deviceLabel = groupActive ? `${group.name} 当前已选中` : `切换到 ${group.name} 的第一个终端`
        return (
          <div
            key={group.key}
            className={cn(
              'flex h-[33px] flex-none items-stretch overflow-hidden rounded-[7px] border border-[#26323c] bg-[#0f161d] hover:border-[#354451]',
              groupActive && 'border-[#315a48] bg-[#122019] shadow-[inset_0_-2px_var(--color-primary)]',
              'max-md:h-auto max-md:max-w-[46vw] max-md:flex-col',
            )}
          >
            <button
              title={deviceLabel}
              aria-label={deviceLabel}
              onClick={() => { if (!groupActive) onSelect(group.sessions[0].id) }}
              className={cn(
                'flex max-w-[150px] min-w-0 cursor-pointer items-center gap-[7px] border-r border-[#26323c] bg-transparent px-2.5 hover:bg-[#17212a] max-md:max-w-none max-md:border-r-0 max-md:border-b max-md:px-2 max-md:py-[2px]',
                groupActive && 'cursor-default hover:bg-transparent',
              )}
            >
              <i
                data-active={group.sessions.some((session) => session.active)}
                className="size-1.5 flex-none rounded-full bg-[#59636d] data-[active=true]:bg-primary data-[active=true]:shadow-[0_0_8px_#77f2b488]"
              />
              <strong className={cn('truncate text-[10px] font-semibold text-[#b9c5ce] max-md:text-[9px]', groupActive && 'text-[#edf4f9]')}>
                {group.name}
              </strong>
            </button>
            <div className="flex items-stretch">
            <span className="flex items-stretch">
              {group.sessions.map((session, sessionIndex) => {
                const sessionActive = session.id === activeId
                const onlineCount = session.services.filter((service) => service.status === 'online').length
                return (
                  <span
                    key={session.id}
                    title={session.id}
                    className={cn(
                      'flex items-stretch border-r border-[#222d36] bg-[#0d1319]',
                      sessionActive && 'bg-[#193025] shadow-[inset_0_-2px_var(--color-primary)]',
                    )}
                  >
                    <button
                      onClick={() => onSelect(session.id)}
                      aria-label={`切换到 ${group.name} 终端 ${session.id.slice(0, 8)}`}
                      className={cn(
                        'flex cursor-pointer items-center gap-1 bg-transparent pr-[7px] pl-[3px] text-[#667581] hover:bg-[#17212a] hover:text-[#b9c7d0] max-md:pr-[5px] max-md:pl-[2px]',
                        sessionActive && 'text-primary hover:text-primary',
                      )}
                    >
                      <span className="relative my-auto flex-none">
                        <SessionSprite session={session} pose={sessionIndex === 0 ? 'sit' : 'stand'} />
                        {onlineCount > 0 && (
                          <i
                            aria-label={`${onlineCount} 个开发服务运行中`}
                            className="absolute -top-1 -right-1.5 grid min-w-3 place-items-center rounded-full bg-primary px-[3px] font-mono text-[7px] leading-[11px] font-bold not-italic text-[#07120d] shadow-[0_1px_4px_#000c]"
                          >
                            {onlineCount}
                          </i>
                        )}
                      </span>
                      <code className="font-mono text-[8px] max-md:hidden">{session.id.slice(0, 6)}</code>
                    </button>
                    <button
                      aria-label={`关闭 ${group.name} 终端 ${session.id.slice(0, 8)}`}
                      onClick={() => onClose(session)}
                      className="grid w-5 cursor-pointer place-items-center bg-transparent text-[#586570] hover:bg-[#362027] hover:text-[#ff8d98] max-md:w-[18px]"
                    >
                      <XIcon className="size-2.5" />
                    </button>
                  </span>
                )
              })}
            </span>
            {group.deviceId && (
              <>
                <button
                  aria-label={`预览 ${group.name} 的开发服务`}
                  title="预览设备本地开发服务"
                  onClick={() => onPreview(cloneSource)}
                  className="grid w-7 cursor-pointer place-items-center border-r border-[#26323c] bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary"
                >
                  <MonitorPlay className="size-3.5" />
                </button>
                <button
                  disabled={cloningId !== null}
                  aria-label={`在 ${group.name} 新建终端`}
                  title="在同一设备新建终端"
                  onClick={() => onClone(cloneSource)}
                  className="grid w-7 cursor-pointer place-items-center bg-transparent text-[#709b88] hover:bg-[#193025] hover:text-primary disabled:cursor-wait disabled:text-[#536159] disabled:hover:bg-transparent"
                >
                  {cloningId && group.sessions.some((session) => session.id === cloningId)
                    ? <LoaderCircle className="size-3 animate-spin" />
                    : <Plus className="size-3.5" />}
                </button>
              </>
            )}
            </div>
          </div>
        )
      })}
    </nav>
  )
}
