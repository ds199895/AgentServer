import type { TerminalSession } from '@/api'

export type TerminalGroup = {
  key: string
  deviceId: string | null
  name: string
  sessions: TerminalSession[]
}

export type TerminalDisplay = {
  label: string
  workspaceLabel: string
  groupName: string
  ordinal: number
  shortId: string
  searchText: string
}

const SERVICE_STATUS_TEXT: Record<string, string> = {
  online: 'online 在线 运行中',
  checking: 'checking 检查中',
  offline: 'offline 离线 已停止',
}

function clean(value: string | null | undefined): string {
  return (value || '').trim()
}

export function terminalWorkspaceLabel(session: TerminalSession): string {
  const raw = clean(session.workspace?.root) || clean(session.cwd)
  if (!raw || raw === '.') return '默认目录'
  const withoutTrailingSeparators = raw.replace(/[\\/]+$/, '') || raw
  if (/^[A-Za-z]:$/.test(withoutTrailingSeparators)) return `${withoutTrailingSeparators}\\`
  const parts = withoutTrailingSeparators.split(/[\\/]+/).filter(Boolean)
  return parts.at(-1) || withoutTrailingSeparators
}

/** Group remote sessions by physical device and all local sessions under 本机. */
export function groupTerminalSessions(sessions: TerminalSession[]): TerminalGroup[] {
  const groups = new Map<string, TerminalGroup>()
  sessions.forEach((session) => {
    const deviceId = clean(session.device_id) || null
    const key = deviceId ? `device:${deviceId}` : 'local'
    const existing = groups.get(key)
    if (existing) {
      existing.sessions.push(session)
      return
    }
    groups.set(key, {
      key,
      deviceId,
      name: deviceId ? clean(session.device_name) || clean(session.name) || deviceId : '本机',
      sessions: [session],
    })
  })
  return [...groups.values()]
}

/** Build stable, human-readable labels and broad client-side search text. */
export function buildTerminalDisplayMap(sessions: TerminalSession[]): Map<string, TerminalDisplay> {
  const result = new Map<string, TerminalDisplay>()
  for (const group of groupTerminalSessions(sessions)) {
    group.sessions.forEach((session, index) => {
      const ordinal = index + 1
      const shortId = session.id.slice(0, 8)
      const workspaceLabel = terminalWorkspaceLabel(session)
      const sessionName = clean(session.name)
      const nameRepeatsGroup = sessionName.localeCompare(group.name, undefined, { sensitivity: 'accent' }) === 0
      const labelParts: string[] = []
      if (sessionName && !nameRepeatsGroup) labelParts.push(sessionName)
      if (
        workspaceLabel !== '默认目录' &&
        !labelParts.some((part) => part.localeCompare(workspaceLabel, undefined, { sensitivity: 'accent' }) === 0)
      ) {
        labelParts.push(workspaceLabel)
      }
      if (!labelParts.length) labelParts.push(session.device_id ? '终端' : '本地终端')
      const label = `${labelParts.join(' · ')} · #${ordinal}`
      const serviceText = session.services.flatMap((service) => [
        service.label,
        String(service.port),
        `:${service.port}`,
        service.url,
        service.status,
        SERVICE_STATUS_TEXT[service.status] || '',
      ])
      const searchText = [
        group.name,
        group.deviceId || '本机 local localhost',
        sessionName,
        session.id,
        shortId,
        label,
        workspaceLabel,
        session.workspace?.root,
        session.cwd,
        session.kind,
        session.remote_port,
        ordinal,
        `#${ordinal}`,
        session.active ? 'active running online 运行中' : 'stopped exited offline 已退出 已停止',
        ...serviceText,
      ]
        .filter((value) => value !== null && value !== undefined && value !== '')
        .join(' ')
        .toLocaleLowerCase()
      result.set(session.id, {
        label,
        workspaceLabel,
        groupName: group.name,
        ordinal,
        shortId,
        searchText,
      })
    })
  }
  return result
}
