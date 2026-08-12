import { useCallback, useEffect, useRef, useState } from 'react'
import { XIcon } from 'lucide-react'

import { api, type Device, type Preview, type TerminalSession } from '@/api'
import { DeviceDashboard } from '@/components/DeviceDashboard'
import { DeviceDialog } from '@/components/DeviceDialog'
import { DownloadsPage } from '@/components/DownloadsPage'
import { Eyebrow } from '@/components/Eyebrow'
import { Login } from '@/components/Login'
import { PasswordDialog } from '@/components/PasswordDialog'
import { PreviewDialog } from '@/components/PreviewDialog'
import { PreviewPane } from '@/components/PreviewPane'
import { TerminalEmpty } from '@/components/TerminalEmpty'
import { TerminalRoomOverview } from '@/components/TerminalRoomOverview'
import { TerminalTabsBar } from '@/components/TerminalTabsBar'
import { Topbar, type MainPage } from '@/components/Topbar'
import TerminalPane from '@/TerminalPane'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

const LAST_TERMINAL_KEY = 'agentserver:last-terminal-id'

function storedTerminalId(): string | null {
  try {
    return window.localStorage.getItem(LAST_TERMINAL_KEY)
  } catch {
    return null
  }
}

function storeTerminalId(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(LAST_TERMINAL_KEY, id)
    else window.localStorage.removeItem(LAST_TERMINAL_KEY)
  } catch {
    // Routing still works when browser storage is unavailable.
  }
}

function routeFromLocation(): { page: MainPage; terminalId: string | null } {
  const terminalMatch = window.location.pathname.match(/^\/terminal\/([^/]+)\/?$/)
  if (terminalMatch) {
    try {
      return { page: 'terminals', terminalId: decodeURIComponent(terminalMatch[1]) }
    } catch {
      return { page: 'terminals', terminalId: terminalMatch[1] }
    }
  }
  if (/^\/terminals\/?$/.test(window.location.pathname)) {
    return { page: 'terminals', terminalId: null }
  }
  if (/^\/setup\/?$/.test(window.location.pathname)) {
    return { page: 'setup', terminalId: null }
  }
  return { page: 'devices', terminalId: null }
}

export default function App() {
  const [username, setUsername] = useState<string | null | undefined>(undefined)
  const [devices, setDevices] = useState<Device[]>([])
  const [sessions, setSessions] = useState<TerminalSession[]>([])
  const [previews, setPreviews] = useState<Preview[]>([])
  const [activeId, setActiveId] = useState<string | null>(() => routeFromLocation().terminalId)
  const [page, setPage] = useState<MainPage>(() => routeFromLocation().page)
  const [missingTerminalId, setMissingTerminalId] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const [deviceDialog, setDeviceDialog] = useState<Device | 'new' | null>(null)
  const [previewTarget, setPreviewTarget] = useState<{ deviceId?: string; terminalId?: string } | null>(null)
  const [activePreview, setActivePreview] = useState<{ preview: Preview; url: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [cloningId, setCloningId] = useState<string | null>(null)
  const [closeTarget, setCloseTarget] = useState<TerminalSession | null>(null)
  const [error, setError] = useState('')
  const activeIdRef = useRef<string | null>(routeFromLocation().terminalId)
  const lastTerminalIdRef = useRef<string | null>(routeFromLocation().terminalId || storedTerminalId())

  const load = useCallback(async () => {
    const [nextDevices, nextSessions, nextPreviews] = await Promise.all([api.devices(), api.terminals(), api.previews()])
    setDevices(nextDevices)
    setSessions(nextSessions)
    setPreviews(nextPreviews)
    const route = routeFromLocation()
    const routedSession = route.terminalId ? nextSessions.find((item) => item.id === route.terminalId) : undefined
    const rememberedSession = nextSessions.find((item) => item.id === lastTerminalIdRef.current)
    const preferredSession = routedSession || rememberedSession || nextSessions[0]
    lastTerminalIdRef.current = preferredSession?.id ?? null
    storeTerminalId(lastTerminalIdRef.current)
    if (route.page === 'terminals' && route.terminalId) {
      activeIdRef.current = routedSession?.id ?? null
      setActiveId(routedSession?.id ?? null)
      setMissingTerminalId(routedSession ? null : route.terminalId)
    } else if (route.page === 'terminals') {
      activeIdRef.current = preferredSession?.id ?? null
      setActiveId(preferredSession?.id ?? null)
      setMissingTerminalId(null)
      if (preferredSession) {
        window.history.replaceState({}, '', `/terminal/${encodeURIComponent(preferredSession.id)}`)
      }
    } else if (activeIdRef.current && !nextSessions.some((item) => item.id === activeIdRef.current)) {
      activeIdRef.current = null
      setActiveId(null)
      setMissingTerminalId(null)
    }
  }, [])

  useEffect(() => { api.me().then(({ username: name }) => { setUsername(name); return load() }).catch(() => setUsername(null)) }, [load])
  useEffect(() => {
    if (!username) return
    const timer = window.setInterval(() => void load().catch(() => undefined), 5000)
    return () => window.clearInterval(timer)
  }, [load, username])
  useEffect(() => {
    const onPopState = () => {
      const route = routeFromLocation()
      activeIdRef.current = route.terminalId
      if (route.terminalId) {
        lastTerminalIdRef.current = route.terminalId
        storeTerminalId(route.terminalId)
      }
      setActiveId(route.terminalId)
      setMissingTerminalId(null)
      setPage(route.page)
      void load().catch(() => undefined)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [load])

  const showTerminal = (id: string | null, replace = false) => {
    activeIdRef.current = id; setActiveId(id); setMissingTerminalId(null); setPage('terminals')
    lastTerminalIdRef.current = id
    storeTerminalId(id)
    const path = id ? `/terminal/${encodeURIComponent(id)}` : '/terminals'
    if (window.location.pathname !== path) window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
  }
  const navigate = (nextPage: MainPage, replace = false) => {
    if (nextPage === 'terminals') {
      const preferredSession = sessions.find((item) => item.id === lastTerminalIdRef.current) || sessions[0]
      showTerminal(preferredSession?.id ?? null, replace)
      return
    }
    activeIdRef.current = null; setActiveId(null); setMissingTerminalId(null); setPage(nextPage)
    const path = nextPage === 'setup' ? '/setup' : '/'
    if (window.location.pathname !== path) window.history[replace ? 'replaceState' : 'pushState']({}, '', path)
  }
  const openTerminal = async (device: Device) => {
    setBusyId(device.id); setError('')
    try {
      const session = await api.createDeviceTerminal(device.id, device.name)
      setSessions((current) => [...current, session]); showTerminal(session.id)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法打开终端') }
    finally { setBusyId(null) }
  }
  const cloneTerminal = async (source: TerminalSession) => {
    if (!source.device_id) return
    setCloningId(source.id); setError('')
    try {
      const session = await api.createDeviceTerminal(source.device_id, source.device_name || source.name)
      setSessions((current) => [...current, session]); showTerminal(session.id)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法克隆终端') }
    finally { setCloningId(null) }
  }
  const closeTerminal = async () => {
    const session = closeTarget
    if (!session) return
    setCloseTarget(null)
    await api.deleteTerminal(session.id)
    const remaining = sessions.filter((item) => item.id !== session.id)
    setSessions(remaining)
    if (activeIdRef.current === session.id) {
      const sameDevice = session.device_id
        ? remaining.filter((item) => item.device_id === session.device_id)
        : []
      const closedIndex = sessions.findIndex((item) => item.id === session.id)
      const nextSession = sameDevice[0] || remaining[Math.min(closedIndex, remaining.length - 1)]
      showTerminal(nextSession?.id ?? null, true)
    }
  }
  const sync = async () => { setError(''); try { await api.syncDevices(); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '同步失败') } }
  const probe = async (device: Device) => { setBusyId(device.id); try { await api.probeDevice(device.id); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '检测失败') } finally { setBusyId(null) } }
  const remove = async (device: Device) => { if (!window.confirm(`删除设备 ${device.name}？`)) return; try { await api.deleteDevice(device.id); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '删除失败') } }
  const logout = async () => { await api.logout(); setUsername(null); setDevices([]); setSessions([]); setPreviews([]); setActivePreview(null) }
  const stopPreview = async (preview: Preview) => {
    try {
      await api.deletePreview(preview.id)
      setPreviews((current) => current.filter((item) => item.id !== preview.id))
      if (activePreview?.preview.id === preview.id) setActivePreview(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法关闭预览') }
  }

  if (username === undefined) {
    return (
      <div className="grid h-full w-full place-items-center bg-background">
        <div className="h-6 w-3 animate-blink bg-primary" />
      </div>
    )
  }
  if (username === null) return <Login onLogin={(name) => { setUsername(name); void load() }} />
  const activeSession = sessions.find((item) => item.id === activeId)
  const showTerminalTabs = page === 'terminals' && sessions.length > 0
  return (
    <main className="grid h-full w-full grid-rows-[58px_minmax(0,1fr)] bg-background">
      <Topbar
        username={username}
        page={page}
        deviceSummary={`${devices.filter((device) => device.frp_online).length}/${devices.length}`}
        sessionCount={sessions.length}
        onNavigate={navigate}
        onShowPassword={() => setShowPassword(true)}
        onLogout={() => void logout()}
      />
      <section className={cn('grid min-h-0 min-w-0', showTerminalTabs ? 'grid-rows-[44px_minmax(0,1fr)] max-md:grid-rows-[auto_minmax(0,1fr)]' : 'grid-rows-[minmax(0,1fr)]')}>
        {showTerminalTabs && (
          <TerminalTabsBar
            sessions={sessions}
            activeId={activeId}
            cloningId={cloningId}
            onSelect={(id) => showTerminal(id)}
            onClose={setCloseTarget}
            onClone={(source) => void cloneTerminal(source)}
            onPreview={(source) => setPreviewTarget({ deviceId: source.device_id || undefined, terminalId: source.id })}
          />
        )}
        <div className="relative min-h-0 min-w-0 overflow-auto">
          {sessions.length > 0 && (
            <div className={cn(
              'absolute inset-3 overflow-hidden rounded-[10px] border border-[#222d37] bg-[#0b0f14] shadow-[0_20px_55px_#0006] max-md:inset-1.5',
              !(page === 'terminals' && activeSession) && 'invisible pointer-events-none',
            )}>
              {sessions.map((session) => (
                <TerminalPane key={session.id} sessionId={session.id} visible={page === 'terminals' && session.id === activeId} />
              ))}
            </div>
          )}
          {page === 'terminals' && activeSession && (
            <TerminalRoomOverview
              devices={devices}
              sessions={sessions}
              busyId={busyId}
              activeSessionId={activeId}
              onOpen={(device) => void openTerminal(device)}
              onProbe={(device) => void probe(device)}
              onEdit={setDeviceDialog}
              onSelectTerminal={(sessionId) => showTerminal(sessionId)}
            />
          )}
          {page === 'terminals' ? (
            !activeSession && (
              <TerminalEmpty
                missingTerminalId={missingTerminalId}
                hasSessions={sessions.length > 0}
                onBack={() => navigate('devices')}
              />
            )
          ) : page === 'setup' ? (
            <DownloadsPage devices={devices} />
          ) : (
            <DeviceDashboard
              devices={devices}
              sessions={sessions}
              busyId={busyId}
              onOpen={(device) => void openTerminal(device)}
              onEdit={setDeviceDialog}
              onDelete={(device) => void remove(device)}
              onProbe={(device) => void probe(device)}
              onSync={() => void sync()}
              onAdd={() => navigate('setup')}
              onSelectTerminal={(sessionId) => showTerminal(sessionId)}
              onPreview={(device) => setPreviewTarget({ deviceId: device?.id })}
            />
          )}
        </div>
      </section>
      {error && (
        <button onClick={() => setError('')} className="fixed right-5 bottom-5 flex cursor-pointer items-center gap-4 rounded-lg border border-[#713640] bg-[#28171bdd] px-3.5 py-[11px] text-xs text-[#ffadb5] shadow-[0_14px_40px_#0008] max-md:right-3 max-md:bottom-3 max-md:left-3 max-md:justify-between">
          {error}
          <XIcon className="size-3.5 flex-none" />
        </button>
      )}
      {showPassword && <PasswordDialog onClose={() => setShowPassword(false)} />}
      {closeTarget && (
        <Dialog open onOpenChange={(open) => { if (!open) setCloseTarget(null) }}>
          <DialogContent className="sm:max-w-[410px]">
            <DialogHeader>
              <Eyebrow>TERMINAL</Eyebrow>
              <DialogTitle>关闭终端</DialogTitle>
              <DialogDescription>
                关闭 {closeTarget.name} ({closeTarget.id.slice(0, 8)})？后台会话将被终止，未保存的输出会丢失。
              </DialogDescription>
            </DialogHeader>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setCloseTarget(null)}>取消</Button>
              <Button variant="destructive" onClick={() => void closeTerminal()}>关闭终端</Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
      {deviceDialog && (
        <DeviceDialog
          device={deviceDialog === 'new' ? undefined : deviceDialog}
          onClose={() => setDeviceDialog(null)}
          onSaved={() => void load()}
        />
      )}
      {previewTarget && (
        <PreviewDialog
          devices={devices}
          sessions={sessions}
          previews={previews}
          initialDeviceId={previewTarget.deviceId}
          initialTerminalId={previewTarget.terminalId}
          onClose={() => setPreviewTarget(null)}
          onCreated={(preview, url) => {
            setPreviews((current) => [preview, ...current.filter((item) => item.id !== preview.id)])
            setActivePreview({ preview, url })
            setPreviewTarget(null)
          }}
          onDeleted={(id) => {
            setPreviews((current) => current.filter((item) => item.id !== id))
            if (activePreview?.preview.id === id) setActivePreview(null)
          }}
        />
      )}
      {activePreview && (
        <PreviewPane
          preview={activePreview.preview}
          authorizedUrl={activePreview.url}
          onClose={() => setActivePreview(null)}
          onStop={() => void stopPreview(activePreview.preview)}
        />
      )}
    </main>
  )
}
