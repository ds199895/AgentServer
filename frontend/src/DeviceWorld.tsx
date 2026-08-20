import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowUpRight, LayoutGrid, XIcon } from 'lucide-react'
import type { Device, TerminalSession } from './api'
import { buildScene, PAD, roomLayout, ROOM_H, ROOM_W, WALL, type CharSlot, type RoomModel, type RuntimeRoomSession, type SceneModel, type TerminalExecutionVisual } from './pixel/scene'
import { BubbleTracker, bubbleAlpha, bubblePop, drawBubble } from './pixel/bubbles'
import { eventBubbleForTerminal } from './pixel/execution-bubbles'
import { AGENT_OUTFITS, CHAR_DIMS, getAtlas, getCharacter, getScreen, makeFloorLabel, RACK_LEDS, STATUS_COLOR } from './pixel/sprites'
import { DeviceIcon } from '@/components/device-bits'
import { Eyebrow } from '@/components/Eyebrow'
import { RunStatusBadge } from '@/components/RunStatusBadge'
import { Button } from '@/components/ui/button'
import { useExecutionContext } from '@/execution-context'
import {
  activeAgentForTerminal,
  activeRunForTerminal,
  evidenceFreshness,
  fieldEvidence,
  isTerminalRun,
  runStatusLabel,
} from '@/execution-state'
import { cn } from '@/lib/utils'

type Props = {
  devices: Device[]
  sessions: TerminalSession[]
  runtimeSessions?: RuntimeRoomSession[]
  busyId: string | null
  onOpen: (device: Device) => void
  onProbe: (device: Device) => void
  onEdit: (device: Device) => void
  onSelectTerminal: (sessionId: string) => void
  onSelectRuntime?: (sessionId: string) => void
  compact?: boolean
  // Terminal page only: the session whose tab is currently active gets a
  // highlight ring in the canvas. Omitted on the device-list page.
  activeSessionId?: string | null
}

const INTERIOR_TILES_X = (ROOM_W - WALL * 2) / 16
const INTERIOR_TILES_Y = (ROOM_H - WALL * 2) / 16

type CharacterPick = { room: RoomModel; char: CharSlot }

type HoveredPerson = {
  sessionId: string
  sessionName: string
  deviceName: string
  active: boolean
  agentName: string | null
  agentCwd: string
  runLabel: string | null
  left: number
  top: number
}

function agentDisplayName(kind: string): string {
  return AGENT_OUTFITS[kind]?.name ?? kind
}

function quantizeScale(scale: number): number {
  if (scale < 0.5) return scale
  return Math.max(0.5, Math.round(scale * 4) / 4)
}

export default function DeviceWorld({ devices, sessions, runtimeSessions = [], busyId, onOpen, onProbe, onEdit, onSelectTerminal, onSelectRuntime = () => undefined, compact = false, activeSessionId = null }: Props) {
  const execution = useExecutionContext()
  const hostRef = useRef<HTMLDivElement>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [hoveredName, setHoveredName] = useState('')
  const [hoveredPerson, setHoveredPerson] = useState<HoveredPerson | null>(null)
  // Touch devices get pinch-zoom and a different HUD hint.
  const [coarsePointer] = useState(() => window.matchMedia('(pointer: coarse)').matches)
  const selectedDevice = useMemo(() => devices.find((device) => device.id === selectedId) || null, [devices, selectedId])
  const selectedSessions = useMemo(() => sessions.filter((session) => session.device_id === selectedId), [sessions, selectedId])
  const selectedRecentSession = useMemo(
    () => [...selectedSessions].reverse().find((session) => session.active) || selectedSessions.at(-1) || null,
    [selectedSessions],
  )
  const selectedSession = useMemo(() => sessions.find((session) => session.id === selectedSessionId) || null, [sessions, selectedSessionId])
  const selectedSessionDevice = useMemo(() => devices.find((device) => device.id === selectedSession?.device_id) || null, [devices, selectedSession])
  const executionByTerminal = useMemo(() => {
    const result = new Map<string, TerminalExecutionVisual>()
    if (!execution.snapshot) return result
    for (const session of sessions) {
      const run = activeRunForTerminal(execution.snapshot, session.id)
      const agent = activeAgentForTerminal(execution.snapshot, session.id)
      const event = eventBubbleForTerminal(execution.snapshot, session.id)
      if (!run && !agent) continue
      result.set(session.id, {
        agentKind: agent?.kind || run?.agent_kind || null,
        lifecycle: run?.lifecycle ?? null,
        activity: run?.activity ?? null,
        waitReason: run?.wait_reason ?? null,
        stale: run
          ? !isTerminalRun(run)
            && (run.stale === true
              || evidenceFreshness(fieldEvidence(run, 'activity'), execution.freshness_now) === 'stale')
          : agent?.stale === true,
        agentCwd: agent?.cwd || '',
        runLabel: run ? runStatusLabel(run, execution.freshness_now) : null,
        eventKey: event?.key ?? null,
        eventText: event?.text ?? null,
        eventTone: event?.tone,
      })
    }
    return result
  }, [execution.freshness_now, execution.snapshot, sessions])
  const selectedExecutionRun = selectedSession
    ? activeRunForTerminal(execution.snapshot, selectedSession.id)
    : null
  const selectedExecutionAgent = selectedSession
    ? activeAgentForTerminal(execution.snapshot, selectedSession.id)
    : null
  const selectedAgentKind = selectedExecutionAgent?.kind
    || selectedExecutionRun?.agent_kind
    || selectedSession?.agent?.kind
    || null
  const selectedAgentCwd = selectedExecutionAgent?.cwd || selectedSession?.agent?.cwd || ''
  const sceneVersion = useMemo(() => [
    ...devices.map((device) => `${device.id}:${device.name}:${device.remote_port}:${Number(device.frp_online)}:${Number(device.ssh_available)}:${device.last_error ? 1 : 0}`),
    ...sessions.map((session) => `${session.id}:${session.device_id || ''}:${Number(session.active)}:${session.agent?.kind || ''}`),
    `execution:${execution.snapshot?.as_of_sequence ?? 'legacy'}`,
    `freshness:${execution.freshness_now}`,
  ].join('|'), [devices, execution.freshness_now, execution.snapshot?.as_of_sequence, sessions])
  const scene = useMemo<SceneModel>(() => {
    const host = hostRef.current
    const aspect = host ? host.clientWidth / Math.max(1, host.clientHeight) : 1.6
    return buildScene(devices, sessions, aspect, executionByTerminal, runtimeSessions)
    // sceneVersion captures every input that affects layout
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runtimeSessions, sceneVersion])

  // Latest values the render loop needs without re-running the effect.
  const busyIdRef = useRef(busyId)
  const selectedIdRef = useRef(selectedId)
  const selectedSessionIdRef = useRef(selectedSessionId)
  const activeSessionIdRef = useRef(activeSessionId)
  const hoveredRoomIdRef = useRef<string | null>(null)
  const hoveredSessionIdRef = useRef<string | null>(null)
  // Survives canvas rebuilds so each execution event only pops once.
  const bubbleTrackerRef = useRef(new BubbleTracker())
  useEffect(() => { busyIdRef.current = busyId }, [busyId])
  useEffect(() => { selectedIdRef.current = selectedId }, [selectedId])
  useEffect(() => { selectedSessionIdRef.current = selectedSessionId }, [selectedSessionId])
  useEffect(() => { activeSessionIdRef.current = activeSessionId }, [activeSessionId])

  useEffect(() => {
    if (selectedId && !devices.some((device) => device.id === selectedId)) setSelectedId(null)
  }, [devices, selectedId])
  useEffect(() => {
    if (selectedSessionId && !sessions.some((session) => session.id === selectedSessionId)) setSelectedSessionId(null)
  }, [sessions, selectedSessionId])

  const clearPersonHover = () => {
    hoveredSessionIdRef.current = null
    setHoveredPerson(null)
  }

  useEffect(() => {
    const host = hostRef.current
    bubbleTrackerRef.current.prune(new Set([...sessions.map((session) => session.id), ...runtimeSessions.map((session) => session.id)]))
    if (!host || !scene.rooms.length) return

    const atlas = getAtlas()
    const canvas = document.createElement('canvas')
    canvas.className = 'block size-full touch-none outline-none'
    canvas.style.cursor = 'grab'
    host.appendChild(canvas)
    const context = canvas.getContext('2d')!
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    const floorLabels = new Map<string, HTMLCanvasElement>()
    scene.rooms.forEach((room) => {
      floorLabels.set(room.device.id, makeFloorLabel(room.device.name, room.device.remote_port, room.status))
    })

    let cssW = 0
    let cssH = 0
    let dpr = Math.min(window.devicePixelRatio || 1, 2)
    const camera = { cx: scene.worldW / 2, cy: scene.worldH / 2, scale: 1, fit: 1 }
    // extra top margin keeps the HUD clear of the first row of nameplates
    const TOP_PAD = compact ? 3 : 92
    const BOTTOM_PAD = compact ? 3 : 26
    const SIDE_PAD = compact ? 6 : 40
    // The full scene includes an apron around the building. In the thumbnail,
    // fit the rooms themselves so the canvas reads at a glance instead of
    // spending most of its area on decorative outer space.
    const fitWorldW = compact ? Math.max(1, scene.worldW - PAD * 2) : scene.worldW
    const fitWorldH = compact ? Math.max(1, scene.worldH - PAD * 2) : scene.worldH
    const biasedCenterY = (scale: number) => scene.worldH / 2 - (TOP_PAD - BOTTOM_PAD) / (2 * scale)

    const clampCamera = () => {
      const margin = 80
      const halfW = cssW / (2 * camera.scale)
      const halfH = cssH / (2 * camera.scale)
      if (halfW * 2 >= scene.worldW + margin) camera.cx = scene.worldW / 2
      else camera.cx = Math.min(scene.worldW + margin / 2 - halfW, Math.max(halfW - margin / 2, camera.cx))
      if (halfH * 2 >= scene.worldH + margin) camera.cy = biasedCenterY(camera.scale)
      else camera.cy = Math.min(scene.worldH + margin / 2 - halfH, Math.max(halfH - margin / 2, camera.cy))
    }

    const fitCamera = () => {
      const scale = Math.min((cssW - SIDE_PAD) / fitWorldW, (cssH - TOP_PAD - BOTTOM_PAD) / fitWorldH)
      camera.fit = quantizeScale(scale)
      camera.scale = camera.fit
      camera.cx = scene.worldW / 2
      camera.cy = biasedCenterY(camera.scale)
      clampCamera()
    }

    const resize = () => {
      cssW = host.clientWidth
      cssH = host.clientHeight
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.max(1, Math.round(cssW * dpr))
      canvas.height = Math.max(1, Math.round(cssH * dpr))
      canvas.style.width = `${cssW}px`
      canvas.style.height = `${cssH}px`
      const scale = Math.min((cssW - SIDE_PAD) / fitWorldW, (cssH - TOP_PAD - BOTTOM_PAD) / fitWorldH)
      camera.fit = quantizeScale(scale)
      if (camera.scale < camera.fit) camera.scale = camera.fit
      clampCamera()
    }
    resize()
    fitCamera()
    const resizeObserver = new ResizeObserver(resize)
    resizeObserver.observe(host)

    // ------------------------------------------------------------- picking

    const worldPoint = (clientX: number, clientY: number) => {
      const bounds = canvas.getBoundingClientRect()
      return {
        x: camera.cx + (clientX - bounds.left - cssW / 2) / camera.scale,
        y: camera.cy + (clientY - bounds.top - cssH / 2) / camera.scale,
      }
    }

    const canvasPoint = (worldX: number, worldY: number) => ({
      x: cssW / 2 + (worldX - camera.cx) * camera.scale,
      y: cssH / 2 + (worldY - camera.cy) * camera.scale,
    })

    const pickCharacter = (clientX: number, clientY: number): CharacterPick | null => {
      const point = worldPoint(clientX, clientY)
      // Keep people easy to target at fit-to-screen scale while preserving
      // distinct hit areas after zooming in.
      const padding = Math.max(4, 8 / camera.scale)
      for (let roomIndex = scene.rooms.length - 1; roomIndex >= 0; roomIndex -= 1) {
        const room = scene.rooms[roomIndex]
        for (let charIndex = room.chars.length - 1; charIndex >= 0; charIndex -= 1) {
          const char = room.chars[charIndex]
          const width = CHAR_DIMS[char.pose].w
          const height = CHAR_DIMS[char.pose].h
          if (
            point.x >= room.x + char.x - padding && point.x <= room.x + char.x + width + padding
            && point.y >= room.y + char.y - padding && point.y <= room.y + char.y + height + padding
          ) return { room, char }
        }
      }
      return null
    }

    const pickRoom = (clientX: number, clientY: number): RoomModel | null => {
      const point = worldPoint(clientX, clientY)
      for (const room of scene.rooms) {
        if (point.x >= room.x - 4 && point.x <= room.x + ROOM_W + 4 && point.y >= room.y - 4 && point.y <= room.y + ROOM_H + 4) return room
      }
      return null
    }

    const updateHover = (event: PointerEvent) => {
      const person = pickCharacter(event.clientX, event.clientY)
      if (person) {
        const session = person.char.kind === 'terminal'
          ? sessions.find((item) => item.id === person.char.sessionId)
          : runtimeSessions.find((item) => item.id === person.char.sessionId)
        const popoverWidth = Math.min(260, Math.max(220, cssW - 18))
        const spriteWidth = CHAR_DIMS[person.char.pose].w
        const anchor = canvasPoint(
          person.room.x + person.char.x + spriteWidth / 2,
          person.room.y + person.char.y,
        )
        hoveredRoomIdRef.current = null
        hoveredSessionIdRef.current = person.char.sessionId
        setHoveredName('')
        const nextHoveredPerson = {
          sessionId: person.char.sessionId,
          sessionName: person.char.name || (person.char.kind === 'runtime' ? `Runtime ${person.char.sessionId.slice(0, 8)}` : `终端 ${person.char.sessionId.slice(0, 8)}`),
          deviceName: person.room.device.name,
          active: person.char.active,
          agentName: person.char.agent ? agentDisplayName(person.char.agent) : null,
          agentCwd: person.char.agentCwd,
          runLabel: person.char.runLabel,
          left: Math.min(cssW - popoverWidth / 2 - 9, Math.max(popoverWidth / 2 + 9, anchor.x)),
          top: anchor.y - Math.max(5, 4 * camera.scale),
        }
        setHoveredPerson((current) => (
          current
          && current.sessionId === nextHoveredPerson.sessionId
          && current.sessionName === nextHoveredPerson.sessionName
          && current.deviceName === nextHoveredPerson.deviceName
          && current.active === nextHoveredPerson.active
          && current.agentName === nextHoveredPerson.agentName
          && current.agentCwd === nextHoveredPerson.agentCwd
          && current.runLabel === nextHoveredPerson.runLabel
          && current.left === nextHoveredPerson.left
          && current.top === nextHoveredPerson.top
            ? current
            : nextHoveredPerson
        ))
        canvas.style.cursor = 'pointer'
        return
      }
      const room = pickRoom(event.clientX, event.clientY)
      hoveredSessionIdRef.current = null
      hoveredRoomIdRef.current = room?.device.id || null
      setHoveredPerson(null)
      setHoveredName(room?.device.name || '')
      canvas.style.cursor = room ? 'pointer' : 'grab'
    }

    let dragStart: { x: number; y: number } | null = null
    let dragging = false
    // Active pointers by id; two pointers means a pinch gesture.
    const pointers = new Map<number, { x: number; y: number }>()
    let pinchDistance = 0

    const clearHoverState = () => {
      hoveredRoomIdRef.current = null
      hoveredSessionIdRef.current = null
      setHoveredName('')
      setHoveredPerson(null)
    }

    const zoomAt = (clientX: number, clientY: number, nextScale: number) => {
      const bounds = canvas.getBoundingClientRect()
      const mx = clientX - bounds.left
      const my = clientY - bounds.top
      const wx = camera.cx + (mx - cssW / 2) / camera.scale
      const wy = camera.cy + (my - cssH / 2) / camera.scale
      camera.scale = nextScale
      camera.cx = wx - (mx - cssW / 2) / nextScale
      camera.cy = wy - (my - cssH / 2) / nextScale
      clampCamera()
    }

    const onPointerDown = (event: PointerEvent) => {
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      canvas.setPointerCapture(event.pointerId)
      if (pointers.size === 2) {
        const [first, second] = [...pointers.values()]
        pinchDistance = Math.hypot(first.x - second.x, first.y - second.y)
        dragStart = null
        dragging = false
        clearHoverState()
        canvas.style.cursor = 'grabbing'
        return
      }
      dragStart = { x: event.clientX, y: event.clientY }
      dragging = false
    }
    const onPointerMove = (event: PointerEvent) => {
      if (!pointers.has(event.pointerId)) {
        // Hover tracking only matters for an unpressed (mouse) pointer.
        updateHover(event)
        return
      }
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
      if (pointers.size === 2) {
        const [first, second] = [...pointers.values()]
        const distance = Math.hypot(first.x - second.x, first.y - second.y)
        const midX = (first.x + second.x) / 2
        const midY = (first.y + second.y) / 2
        if (pinchDistance > 0 && distance > 0) {
          // Raw factor while moving for smoothness; quantized on release.
          const next = Math.min(6, Math.max(camera.fit, camera.scale * (distance / pinchDistance)))
          if (next !== camera.scale) zoomAt(midX, midY, next)
        }
        pinchDistance = distance
        return
      }
      if (dragStart) {
        const dx = event.clientX - dragStart.x
        const dy = event.clientY - dragStart.y
        if (!dragging && Math.hypot(dx, dy) > 5) {
          dragging = true
          clearHoverState()
          canvas.style.cursor = 'grabbing'
        }
        if (dragging) {
          camera.cx -= dx / camera.scale
          camera.cy -= dy / camera.scale
          dragStart = { x: event.clientX, y: event.clientY }
          clampCamera()
          return
        }
      }
      updateHover(event)
    }
    const onPointerUp = (event: PointerEvent) => {
      const wasPinch = pointers.size > 1
      pointers.delete(event.pointerId)
      if (wasPinch) {
        pinchDistance = 0
        dragging = false
        if (pointers.size === 0) {
          // Snap to a crisp pixel-art scale once the gesture ends.
          camera.scale = quantizeScale(camera.scale)
          clampCamera()
          canvas.style.cursor = 'grab'
        } else {
          // Resume single-finger panning from the remaining finger.
          const remaining = [...pointers.values()][0]
          dragStart = remaining ? { x: remaining.x, y: remaining.y } : null
        }
        return
      }
      if (dragStart && !dragging) {
        const person = pickCharacter(event.clientX, event.clientY)
        if (person) {
          if (person.char.kind === 'runtime') onSelectRuntime(person.char.sessionId)
          else {
            setSelectedSessionId(person.char.sessionId)
            setSelectedId(null)
          }
        } else {
          const room = pickRoom(event.clientX, event.clientY)
          if (room) {
            setSelectedId(room.device.id)
            setSelectedSessionId(null)
          }
        }
      }
      dragStart = null
      dragging = false
      updateHover(event)
    }
    const onPointerCancel = (event: PointerEvent) => {
      pointers.delete(event.pointerId)
      pinchDistance = 0
      dragStart = null
      dragging = false
    }
    const onPointerLeave = (event: PointerEvent) => {
      pointers.delete(event.pointerId)
      const next = event.relatedTarget
      if (next instanceof Element && next.closest('.world-person-popover')) return
      clearHoverState()
      dragStart = null
      dragging = false
      pinchDistance = 0
    }
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      clearHoverState()
      const factor = event.deltaY < 0 ? 1.25 : 0.8
      const next = Math.min(6, Math.max(camera.fit, quantizeScale(camera.scale * factor)))
      if (next === camera.scale) return
      zoomAt(event.clientX, event.clientY, next)
    }
    const onDoubleClick = () => {
      clearHoverState()
      fitCamera()
    }

    if (!compact) {
      canvas.addEventListener('pointerdown', onPointerDown)
      canvas.addEventListener('pointermove', onPointerMove)
      canvas.addEventListener('pointerup', onPointerUp)
      canvas.addEventListener('pointercancel', onPointerCancel)
      canvas.addEventListener('pointerleave', onPointerLeave)
      canvas.addEventListener('wheel', onWheel, { passive: false })
      canvas.addEventListener('dblclick', onDoubleClick)
    }

    // ------------------------------------------------------------ drawing

    const glow = (x: number, y: number, radius: number, color: string, alpha: number) => {
      const gradient = context.createRadialGradient(x, y, 0, x, y, radius)
      gradient.addColorStop(0, color)
      gradient.addColorStop(1, 'rgba(0,0,0,0)')
      context.save()
      context.globalCompositeOperation = 'lighter'
      context.globalAlpha = alpha
      context.fillStyle = gradient
      context.fillRect(x - radius, y - radius, radius * 2, radius * 2)
      context.restore()
    }

    const drawWalls = (room: RoomModel) => {
      const { x, y } = room
      context.fillStyle = '#1a232e'
      context.fillRect(x, y, ROOM_W, WALL)
      context.fillRect(x, y + ROOM_H - WALL, ROOM_W, WALL)
      context.fillRect(x, y + WALL, WALL, ROOM_H - WALL * 2)
      context.fillRect(x + ROOM_W - WALL, y + WALL, WALL, ROOM_H - WALL * 2)
      // top highlight + inner shadow give the wall a lit edge
      context.fillStyle = '#2b3846'
      context.fillRect(x, y, ROOM_W, 2)
      context.fillStyle = '#101820'
      context.fillRect(x, y + WALL - 1, ROOM_W, 1)
      context.fillRect(x, y + ROOM_H - WALL, ROOM_W, 1)
      // panel seams
      context.fillStyle = '#141c26'
      for (let seam = 16; seam < ROOM_W; seam += 16) {
        context.fillRect(x + seam, y + 2, 1, WALL - 3)
        context.fillRect(x + seam, y + ROOM_H - WALL + 1, 1, WALL - 2)
      }
      for (let seam = 24; seam < ROOM_H - WALL; seam += 16) {
        context.fillRect(x + 1, y + seam, WALL - 2, 1)
        context.fillRect(x + ROOM_W - WALL + 1, y + seam, WALL - 2, 1)
      }
      // corner caps
      context.fillStyle = '#232f3c'
      for (const [cx, cy] of [[0, 0], [ROOM_W - WALL, 0], [0, ROOM_H - WALL], [ROOM_W - WALL, ROOM_H - WALL]]) {
        context.fillRect(x + cx + 1, y + cy + 1, WALL - 2, WALL - 2)
      }
    }

    const drawRoom = (room: RoomModel, time: number, phase: number, wallTime: number) => {
      const { x, y } = room
      const layout = roomLayout(room)
      const lit = room.status !== 'offline'
      const accent = STATUS_COLOR[room.status]

      // floor
      const floor = atlas.floors[room.floorVariant]
      for (let ty = 0; ty < INTERIOR_TILES_Y; ty += 1) {
        for (let tx = 0; tx < INTERIOR_TILES_X; tx += 1) {
          context.drawImage(floor, x + WALL + tx * 16, y + WALL + ty * 16)
        }
      }

      if (lit) glow(x + ROOM_W / 2, y + 78, 130, '#ffd9a0', 0.18)

      context.drawImage(atlas.rug, x + 80, y + 96)

      drawWalls(room)
      if (layout.windowX !== null) context.drawImage(atlas.windows[room.windowVariant], x + layout.windowX, y - 2)
      if (layout.acX !== null) context.drawImage(atlas.ac, x + layout.acX, y + 1)
      context.drawImage(atlas.posters[room.posterVariant], x + layout.posterX, y + 1)

      // status light strip on the floor near the bottom wall
      const pulse = lit ? 0.6 + 0.25 * Math.sin(time * 2 + phase) : 0.25 + 0.12 * Math.sin(time * 0.8 + phase)
      context.globalAlpha = pulse
      context.fillStyle = accent
      context.fillRect(x + ROOM_W / 2 - 12, y + ROOM_H - WALL - 5, 24, 3)
      context.globalAlpha = 1
      glow(x + ROOM_W / 2, y + ROOM_H - WALL - 7, 20, accent, lit ? 0.18 : 0.07)

      // furniture (with contact shadows to ground them)
      context.fillStyle = 'rgba(0,0,0,.28)'
      context.fillRect(x + layout.deskX + 2, y + layout.deskY + 23, 46, 4)
      context.fillRect(x + layout.rackX + 1, y + layout.rackY + 31, 15, 3)
      if (layout.shelfX !== null) context.fillRect(x + layout.shelfX + 1, y + 37, 23, 3)
      context.drawImage(atlas.desks[room.metalDesk ? 1 : 0], x + layout.deskX, y + layout.deskY)
      context.drawImage(atlas.monitor, x + layout.monitorX, y + layout.monitorY)
      context.drawImage(getScreen(room.status, Math.floor(time * 2 + phase) % 2), x + layout.monitorX + 2, y + layout.monitorY + 2)
      if (lit) glow(x + layout.monitorX + 8, y + layout.monitorY + 6, 38, accent, 0.28)

      context.drawImage(atlas.rack, x + layout.rackX, y + layout.rackY)
      RACK_LEDS.forEach((led, slot) => {
        let on = false
        if (room.status === 'ready') on = Math.sin(time * 3 + phase + slot * 1.7) > -0.2
        else if (room.status === 'partial') on = Math.sin(time * 1.5 + phase + slot) > 0.3
        else on = slot === 0 && time % 2 < 1
        context.fillStyle = on ? accent : '#1e2a24'
        context.fillRect(x + layout.rackX + led.x, y + layout.rackY + led.y, 2, 2)
      })

      context.drawImage(atlas.plants[room.plantVariant], x + layout.plantX, y + layout.plantY)
      if (layout.lampX !== null) {
        context.fillStyle = 'rgba(0,0,0,.28)'
        context.fillRect(x + layout.lampX + 1, y + 131, 11, 3)
        context.drawImage(atlas.lamp, x + layout.lampX, y + 118)
        if (lit) glow(x + layout.lampX + 6, y + 122, 34, '#ffd9a0', 0.24)
      }
      if (layout.shelfX !== null) context.drawImage(atlas.shelf, x + layout.shelfX, y + 14)

      // characters (the sit sprite includes the chair, so the standalone
      // chair is only drawn when no session is seated at the desk)
      const hasSitter = room.chars.some((char) => char.pose === 'sit')
      if (!hasSitter) context.drawImage(atlas.chair, x + layout.chairX, y + layout.chairY)
      room.chars.forEach((char, slotIndex) => {
        const terminalOutcome = char.lifecycle === 'succeeded'
          || char.lifecycle === 'failed'
          || char.lifecycle === 'cancelled'
          || char.lifecycle === 'lost'
        const phaseRate = char.activity === 'coding' || char.activity === 'tooling'
          ? 7
          : char.activity === 'testing'
            ? 5
            : 4
        const freezeCharacter = reduceMotion || char.activity === 'waiting' || char.stale || terminalOutcome
        const sprite = getCharacter(
          char.pose,
          char.variant,
          freezeCharacter ? 0 : Math.floor(time * phaseRate + slotIndex * 1.3) % 2,
          !freezeCharacter && (time + slotIndex * 2.1) % 3.7 < 0.15,
          char.active && char.lifecycle !== 'lost',
          char.agent,
        )
        if (char.pose === 'stand') {
          context.fillStyle = 'rgba(0,0,0,.3)'
          context.beginPath()
          context.ellipse(x + char.x + 8, y + char.y + 21, 7, 2, 0, 0, Math.PI * 2)
          context.fill()
        }
        context.drawImage(sprite, x + char.x, y + char.y)
        const markerX = x + char.x + (char.pose === 'sit' ? 9 : 8)
        const markerY = y + char.y - 5
        context.save()
        if (char.stale) {
          context.fillStyle = '#6f7d89'
          context.beginPath()
          context.arc(markerX, markerY, 5, 0, Math.PI * 2)
          context.fill()
          context.fillStyle = '#10161c'
          context.font = 'bold 7px monospace'
          context.textAlign = 'center'
          context.textBaseline = 'middle'
          context.fillText('?', markerX, markerY + 0.5)
        } else if (char.lifecycle === 'failed' || char.lifecycle === 'lost') {
          context.drawImage(atlas.alert, markerX - 7, markerY - 7, 14, 14)
        } else if (char.lifecycle === 'succeeded') {
          const sparkle = atlas.sparkles[reduceMotion ? 0 : Math.floor(time * 5) % 2]
          context.drawImage(sparkle, markerX - 7, markerY - 7, 14, 14)
        } else if (char.activity === 'thinking' || char.activity === 'planning' || char.activity === 'reviewing') {
          context.fillStyle = '#c9a9f4'
          context.globalAlpha = reduceMotion ? 0.85 : 0.6 + Math.sin(time * 3 + slotIndex) * 0.25
          for (let dot = 0; dot < 3; dot += 1) {
            context.beginPath()
            context.arc(markerX - 4 + dot * 4, markerY, dot === 2 ? 2 : 1.5, 0, Math.PI * 2)
            context.fill()
          }
        } else if (char.activity === 'testing') {
          const radius = reduceMotion ? 6 : 6 + Math.sin(time * 4 + slotIndex) * 1.5
          context.strokeStyle = '#91cce8'
          context.lineWidth = 1.25 / camera.scale
          context.beginPath()
          context.arc(markerX, markerY, radius, 0, Math.PI * 2)
          context.stroke()
        } else if (char.activity === 'waiting') {
          context.fillStyle = '#e9bd68'
          context.fillRect(markerX - 4, markerY - 5, 3, 10)
          context.fillRect(markerX + 1, markerY - 5, 3, 10)
        } else if (char.activity === 'coding' || char.activity === 'tooling') {
          const alpha = reduceMotion ? 0.18 : 0.14 + 0.08 * Math.sin(time * 6 + slotIndex)
          glow(markerX, markerY + 8, 13, '#77f2b4', alpha)
        }
        context.restore()
        if (!compact) {
          // Speech bubble: pops when a hook/reporter event moved this
          // character's state; a pending wait stays pinned until resolved.
          const bubbleState = {
            lifecycle: char.lifecycle,
            activity: char.activity,
            waitReason: char.waitReason,
            stale: char.stale,
            eventKey: char.eventKey,
            eventText: char.eventText,
            eventTone: char.eventTone,
          }
          const bubble = bubbleTrackerRef.current.track(char.sessionId, bubbleState, wallTime)
          if (bubble) {
            const alpha = bubbleAlpha(bubble, bubbleState, wallTime)
            if (alpha > 0) {
              drawBubble(
                context,
                x + char.x + (char.pose === 'sit' ? 9 : 8),
                y + char.y - 1,
                bubble,
                alpha,
                {
                  unit: 1 / camera.scale,
                  clampX0: x + WALL,
                  clampX1: x + ROOM_W - WALL,
                  pop: bubblePop(bubble, wallTime, reduceMotion),
                },
              )
            }
          }
        }
        if (char.sessionId === activeSessionIdRef.current) {
          // pulsing ground ring under the character whose terminal tab is active
          const centerX = char.pose === 'sit' ? char.x + 9 : char.x + 8
          const baseY = char.pose === 'sit' ? char.y + 28 : char.y + 21
          const radiusX = char.pose === 'sit' ? 10 : 9
          const pulse = reduceMotion ? 0.65 : 0.65 + 0.35 * Math.sin(time * 4)
          glow(x + centerX, y + baseY - 5, 16, '#8affd2', 0.22 * pulse)
          context.globalAlpha = 0.45 + 0.4 * pulse
          context.strokeStyle = '#8affd2'
          context.lineWidth = 1.5 / camera.scale
          context.beginPath()
          context.ellipse(x + centerX, y + baseY, radiusX, 3.5, 0, 0, Math.PI * 2)
          context.stroke()
          context.globalAlpha = 1
        }
        if (!char.active) {
          const bob = reduceMotion ? 0 : Math.sin(time * 1.2 + slotIndex) * 1.5
          context.globalAlpha = 0.85
          // sitters are taller and face the desk, so the Zzz floats beside the head
          const zzzY = char.pose === 'sit' ? y + char.y + 1 + bob : y + char.y - 9 + bob
          const zzzX = char.pose === 'sit' ? x + char.x + 16 : x + char.x + 14
          context.drawImage(atlas.zzz, zzzX, zzzY, 10, 10)
          context.globalAlpha = 1
        }
      })

      // info plaque on the floor at the room's center
      const plate = floorLabels.get(room.device.id)!
      context.drawImage(plate, Math.round(x + ROOM_W / 2 - plate.width / 2), y + 64)

      if (!lit) {
        context.fillStyle = 'rgba(7,11,17,.52)'
        context.fillRect(x, y, ROOM_W, ROOM_H)
        context.fillStyle = 'rgba(30,42,58,.18)'
        context.fillRect(x, y, ROOM_W, ROOM_H)
        if (room.device.last_error) {
          const bob = reduceMotion ? 0 : Math.sin(time * 2 + phase) * 2
          context.drawImage(atlas.alert, x + layout.rackX + 9, y + layout.rackY - 12 + bob)
        }
      }

      if (busyIdRef.current === room.device.id) {
        const sparkle = atlas.sparkles[Math.floor(time * 6) % 2]
        context.drawImage(sparkle, Math.round(x + ROOM_W / 2 + plate.width / 2) + 5, y + 61, 18, 18)
      }
    }

    const drawFrame = (now: number) => {
      const time = reduceMotion ? 0 : now / 1000
      context.setTransform(dpr, 0, 0, dpr, 0, 0)
      context.imageSmoothingEnabled = false
      context.fillStyle = '#05090e'
      context.fillRect(0, 0, cssW, cssH)

      const offsetX = cssW / 2 - camera.cx * camera.scale
      const offsetY = cssH / 2 - camera.cy * camera.scale
      context.setTransform(dpr * camera.scale, 0, 0, dpr * camera.scale, Math.round(dpr * offsetX), Math.round(dpr * offsetY))
      context.imageSmoothingEnabled = false

      // facility floor inside the world bounds
      context.save()
      context.beginPath()
      context.rect(0, 0, scene.worldW, scene.worldH)
      context.clip()
      const viewX0 = camera.cx - cssW / (2 * camera.scale) - 32
      const viewY0 = camera.cy - cssH / (2 * camera.scale) - 32
      const viewX1 = camera.cx + cssW / (2 * camera.scale) + 32
      const viewY1 = camera.cy + cssH / (2 * camera.scale) + 32
      const tileStartX = Math.max(0, Math.floor(viewX0 / 512) * 512)
      const tileStartY = Math.max(0, Math.floor(viewY0 / 512) * 512)
      for (let ty = tileStartY; ty < Math.min(scene.worldH, viewY1); ty += 512) {
        for (let tx = tileStartX; tx < Math.min(scene.worldW, viewX1); tx += 512) {
          context.drawImage(atlas.baseFloor, tx, ty)
        }
      }
      context.restore()
      // world edge
      context.strokeStyle = '#1b2836'
      context.lineWidth = 2 / camera.scale
      context.strokeRect(0, 0, scene.worldW, scene.worldH)

      scene.decor.forEach((item) => {
        context.drawImage(item.kind === 'crate' ? atlas.crate : atlas.pipe, item.x, item.y)
      })

      // one soft shadow under the whole contiguous building block
      context.fillStyle = 'rgba(0,0,0,.32)'
      context.beginPath()
      context.roundRect(PAD + 5, PAD + 7, scene.worldW - PAD * 2, scene.worldH - PAD * 2, 8)
      context.fill()

      scene.rooms.forEach((room, index) => drawRoom(room, time, index * 1.37, now / 1000))

      const hovered = hoveredRoomIdRef.current ? scene.rooms.find((room) => room.device.id === hoveredRoomIdRef.current) : null
      if (hovered && hovered.device.id !== selectedIdRef.current) {
        context.strokeStyle = '#77f2b488'
        context.lineWidth = 2 / camera.scale
        context.strokeRect(hovered.x - 3, hovered.y - 3, ROOM_W + 6, ROOM_H + 6)
      }
      const selected = selectedIdRef.current ? scene.rooms.find((room) => room.device.id === selectedIdRef.current) : null
      if (selected) {
        context.strokeStyle = STATUS_COLOR[selected.status]
        context.lineWidth = 2 / camera.scale
        const bracket = 10
        for (const [bx, by, sx, sy] of [
          [selected.x - 5, selected.y - 5, 1, 1],
          [selected.x + ROOM_W + 5, selected.y - 5, -1, 1],
          [selected.x - 5, selected.y + ROOM_H + 5, 1, -1],
          [selected.x + ROOM_W + 5, selected.y + ROOM_H + 5, -1, -1],
        ]) {
          context.beginPath()
          context.moveTo(bx + bracket * sx, by)
          context.lineTo(bx, by)
          context.lineTo(bx, by + bracket * sy)
          context.stroke()
        }
      }

      const drawCharacterTarget = (sessionId: string | null, color: string, selectedTarget: boolean) => {
        if (!sessionId) return
        for (const room of scene.rooms) {
          const char = room.chars.find((item) => item.sessionId === sessionId)
          if (!char) continue
          const width = CHAR_DIMS[char.pose].w
          const height = CHAR_DIMS[char.pose].h
          context.fillStyle = selectedTarget ? `${color}24` : `${color}14`
          context.strokeStyle = color
          context.lineWidth = (selectedTarget ? 2 : 1.25) / camera.scale
          context.beginPath()
          context.roundRect(room.x + char.x - 4, room.y + char.y - 4, width + 8, height + 8, 5)
          context.fill()
          context.stroke()
          return
        }
      }
      if (hoveredSessionIdRef.current !== selectedSessionIdRef.current) drawCharacterTarget(hoveredSessionIdRef.current, '#a8e7ca', false)
      drawCharacterTarget(selectedSessionIdRef.current, '#8affd2', true)
    }

    // 这个循环会重画整个像素世界。终端页常驻挂载着 compact 缩略图，
    // 所以它必须在不可见时彻底停下，可见时也不该跟终端抢满帧。
    const minFrameInterval = compact ? 1000 / 12 : 0
    let frame = 0
    let lastDrawAt = 0
    let onScreen = true
    const animate = (now: number) => {
      frame = window.requestAnimationFrame(animate)
      const terminalInputFocused = compact
        && document.activeElement instanceof Element
        && document.activeElement.closest('.terminal-host') !== null
      // Keep the overview live, but give keyboard echo and xterm rendering
      // priority while a terminal owns focus.
      const interval = terminalInputFocused ? 500 : minFrameInterval
      if (interval && now - lastDrawAt < interval) return
      lastDrawAt = now
      drawFrame(now)
    }
    const startLoop = () => {
      if (!frame) frame = window.requestAnimationFrame(animate)
    }
    const stopLoop = () => {
      if (frame) window.cancelAnimationFrame(frame)
      frame = 0
    }
    // rAF 已经会在标签页隐藏时暂停；IntersectionObserver 负责的是
    // 画布被滚出视口/被收起的情况，那时 rAF 仍在正常触发。
    const visibilityObserver = new IntersectionObserver((entries) => {
      const nextOnScreen = entries[entries.length - 1].isIntersecting
      if (nextOnScreen === onScreen) return
      onScreen = nextOnScreen
      if (onScreen) startLoop()
      else stopLoop()
    })
    visibilityObserver.observe(canvas)
    startLoop()

    return () => {
      stopLoop()
      visibilityObserver.disconnect()
      resizeObserver.disconnect()
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerup', onPointerUp)
      canvas.removeEventListener('pointercancel', onPointerCancel)
      canvas.removeEventListener('pointerleave', onPointerLeave)
      canvas.removeEventListener('wheel', onWheel)
      canvas.removeEventListener('dblclick', onDoubleClick)
      canvas.remove()
    }
  }, [compact, scene])

  if (!devices.length) {
    return compact
      ? (
        <section className="device-world-empty flex h-full min-h-0 w-full flex-col items-center justify-center gap-1.5 text-center text-[#61717e]">
          <div className="grid size-[34px] place-items-center rounded-lg border border-[#315a48] bg-[#12241d] text-primary"><LayoutGrid className="size-[17px]" /></div>
          <p className="m-0 text-[9px]">暂无设备</p>
        </section>
      )
      : (
        <section className="device-world-empty flex h-full min-h-[500px] flex-col items-center justify-center rounded-[14px] border border-[#21303a] bg-[#071018] bg-[radial-gradient(circle_at_50%_45%,#17392c44,transparent_27%)] text-center text-[#61717e]">
          <div className="mb-[18px] grid size-[58px] place-items-center rounded-[14px] border border-[#315a48] bg-[#12241d] text-primary shadow-[0_0_40px_#77f2b424]"><LayoutGrid className="size-7" /></div>
          <h2 className="mt-0 mb-2 text-lg text-[#d7e1e8]">等待房间接入</h2>
          <p className="m-0 max-w-[360px] text-[11px] leading-[1.65]">部署 frpc 并同步设备后，每台设备都会在这里生成一个像素房间。</p>
        </section>
      )
  }

  return (
    <section className={cn(
      'device-world-shell relative isolate min-h-[480px] flex-1 overflow-hidden rounded-[14px] border border-[#21303a] bg-[#05090e] shadow-[0_24px_70px_#0008,inset_0_1px_#ffffff0a] max-md:min-h-[380px] max-md:rounded-[10px]',
      compact && 'is-preview pointer-events-none h-full min-h-0 w-full rounded-none border-0 shadow-none',
    )}>
      <div ref={hostRef} className="absolute inset-0" />
      {!compact && (
        <div className="pointer-events-none absolute top-4 left-4 z-[3] grid grid-cols-[7px_auto] items-center gap-x-2 rounded-lg border border-[#2c414c] bg-[#081118d9] px-3 py-2.5 font-mono text-[9px] font-semibold tracking-[0.14em] text-[#9adfca] shadow-[0_10px_32px_#0007] backdrop-blur-md max-md:top-[9px] max-md:left-[9px]">
          <span className="row-span-2 size-[7px] animate-world-pulse rounded-full bg-primary shadow-[0_0_12px_var(--color-primary)]" />
          PIXEL DEVICE WORLD
          <small className="col-start-2 mt-[5px] font-mono text-[8px] tracking-[0.03em] text-[#607581]">{coarsePointer ? '拖动平移 · 双指缩放 · 点按查看' : '拖动平移 · 滚轮缩放 · 点击房间或小人查看'}</small>
        </div>
      )}
      {!compact && (
        <div className="absolute top-4 right-4 z-[3] flex items-center gap-[13px] rounded-lg border border-[#293b46] bg-[#081118d9] px-[11px] py-[9px] text-[8px] text-[#718390] shadow-[0_10px_32px_#0006] backdrop-blur-md max-md:top-auto max-md:right-[9px] max-md:bottom-[9px] max-md:max-w-[calc(100%-18px)] max-md:gap-2 max-md:overflow-x-auto">
          <span className="flex items-center gap-[5px] whitespace-nowrap"><i className="size-1.5 rounded-full bg-primary shadow-[0_0_8px_#77f2b4aa]" />SSH 可用</span>
          <span className="flex items-center gap-[5px] whitespace-nowrap"><i className="size-1.5 rounded-full bg-[#f7c66f] shadow-[0_0_8px_#f7c66f88]" />仅隧道</span>
          <span className="flex items-center gap-[5px] whitespace-nowrap"><i className="size-1.5 rounded-full bg-[#ff6b7a] shadow-[0_0_8px_#ff6b7a88]" />离线</span>
          <span className="border-l border-[#2b3a44] pl-[11px] text-[#91a0ac] max-md:hidden">小人 = 终端会话</span>
        </div>
      )}
      {!compact && hoveredName && (
        <div className="pointer-events-none absolute bottom-[18px] left-1/2 z-[3] -translate-x-1/2 rounded-md border border-[#365247] bg-[#0a1712e8] px-2.5 py-[7px] font-mono text-[9px] text-[#a8e7ca] shadow-[0_8px_25px_#0008] max-md:hidden">
          查看 {hoveredName}
        </div>
      )}
      {!compact && hoveredPerson && (
        <div
          className="world-person-popover absolute z-[5] grid min-w-[260px] -translate-x-1/2 -translate-y-full grid-cols-[8px_minmax(0,1fr)_auto] items-center gap-[9px] rounded-[9px] border border-[#3b6755] bg-[#09140fef] p-[9px] text-[#dce8e2] shadow-[0_14px_36px_#000c,0_0_0_1px_#77f2b414] backdrop-blur-md after:absolute after:top-full after:left-1/2 after:size-3 after:-translate-x-1/2 after:translate-y-[-7px] after:rotate-45 after:border-r after:border-b after:border-[#3b6755] after:bg-[#09140f] after:content-[''] max-md:min-w-[220px] max-md:max-w-[calc(100%-18px)] max-md:grid-cols-[7px_minmax(0,1fr)_auto]"
          style={{ left: hoveredPerson.left, top: hoveredPerson.top }}
          onPointerLeave={clearPersonHover}
        >
          <span className={cn('size-[7px] rounded-full bg-[#59636d]', hoveredPerson.active && 'bg-primary shadow-[0_0_9px_#77f2b4aa]')} />
          <div className="grid min-w-0 gap-[3px]">
            <strong className="truncate text-[10px]">{hoveredPerson.sessionName}</strong>
            <small className="truncate font-mono text-[8px] text-[#70877c]">{hoveredPerson.deviceName} · {hoveredPerson.sessionId.slice(0, 8)}</small>
            {hoveredPerson.agentName && (
              <small className="truncate font-mono text-[8px] text-[#9adfca]" title={hoveredPerson.agentCwd || undefined}>
                {hoveredPerson.agentName}{hoveredPerson.runLabel ? ` · ${hoveredPerson.runLabel}` : ''}{hoveredPerson.agentCwd ? ` · ${hoveredPerson.agentCwd}` : ''}
              </small>
            )}
          </div>
          <button
            onClick={() => {
              clearPersonHover()
              const person = scene.rooms.flatMap((room) => room.chars).find((char) => char.sessionId === hoveredPerson.sessionId)
              if (person?.kind === 'runtime') onSelectRuntime(hoveredPerson.sessionId)
              else onSelectTerminal(hoveredPerson.sessionId)
            }}
            className="relative z-[1] flex cursor-pointer items-center gap-1 rounded-md border border-[#467b63] bg-primary px-[9px] py-[7px] text-[9px] font-bold whitespace-nowrap text-[#07120d] hover:bg-[#a2ffd0] hover:shadow-[0_6px_18px_#77f2b42b]"
          >
            {scene.rooms.flatMap((room) => room.chars).find((char) => char.sessionId === hoveredPerson.sessionId)?.kind === 'runtime' ? '打开 Runtime' : '打开终端'}
            <ArrowUpRight className="size-3" />
          </button>
        </div>
      )}
      {!compact && selectedSession && (
        <aside className="absolute bottom-4 left-4 z-[4] w-[min(390px,calc(100%-32px))] rounded-[11px] border border-[#3b6755] bg-[#0b1219e8] p-[18px] shadow-[0_20px_55px_#000b] backdrop-blur-lg max-md:right-[9px] max-md:bottom-12 max-md:left-[9px] max-md:w-auto">
          <button onClick={() => setSelectedSessionId(null)} aria-label="关闭终端人物详情" className="absolute top-[9px] right-[9px] grid size-[27px] cursor-pointer place-items-center rounded-md bg-[#17212a] text-[#73818d] transition-colors hover:bg-[#22303a] hover:text-[#edf4f9]"><XIcon className="size-3.5" /></button>
          <Eyebrow>SELECTED PERSON</Eyebrow>
          <div className="mt-1 flex items-center gap-[11px]">
            <DeviceIcon ready={selectedSession.active} partial={false} />
            <div className="min-w-0">
              <h2 className="mt-0 mb-1 truncate text-base text-[#edf4f9]" title={selectedSession.name}>{selectedSession.name}</h2>
              <code className="block truncate font-mono text-[9px] text-[#687985]" title={selectedSession.id}>{selectedSession.id}</code>
            </div>
          </div>
          <div className="mt-[15px] mb-[13px] grid grid-cols-2 gap-1.5">
            <span className="grid min-w-0 gap-1 rounded-[7px] border border-[#223b30] bg-[#0a1410] p-2">
              <small className="text-[8px] text-[#637a6e]">设备</small>
              <strong className="truncate text-[10px] text-[#d7e7df]">{selectedSessionDevice?.name || selectedSession.device_name || '本地'}</strong>
            </span>
            <span className="grid min-w-0 gap-1 rounded-[7px] border border-[#223b30] bg-[#0a1410] p-2">
              <small className="text-[8px] text-[#637a6e]">状态</small>
              <strong className="truncate text-[10px] text-[#d7e7df]">{selectedSession.active ? '运行中' : '已暂停'}</strong>
            </span>
          </div>
          {(selectedExecutionRun || selectedExecutionAgent) && (
            <div className="mt-[-4px] mb-[13px] min-w-0">
              <RunStatusBadge run={selectedExecutionRun} agent={selectedExecutionAgent} />
            </div>
          )}
          {selectedAgentKind && (
            <div className="mt-[-4px] mb-[13px] grid min-w-0 gap-1 rounded-[7px] border border-[#223b30] bg-[#0a1410] p-2">
              <small className="text-[8px] text-[#637a6e]">Agent</small>
              <strong className="truncate text-[10px] text-[#d7e7df]">{agentDisplayName(selectedAgentKind)}</strong>
              {selectedAgentCwd && (
                <code className="block truncate font-mono text-[8px] text-[#687985]" title={selectedAgentCwd}>{selectedAgentCwd}</code>
              )}
            </div>
          )}
          <Button size="sm" className="h-auto px-3 py-[9px] text-[10px] font-bold" onClick={() => onSelectTerminal(selectedSession.id)}>打开对应终端</Button>
        </aside>
      )}
      {!compact && selectedDevice && (
        <aside className="absolute bottom-4 left-4 z-[4] w-[min(390px,calc(100%-32px))] rounded-[11px] border border-[#31464f] bg-[#0b1219e8] p-[18px] shadow-[0_20px_55px_#000b] backdrop-blur-lg max-md:right-[9px] max-md:bottom-12 max-md:left-[9px] max-md:w-auto">
          <button onClick={() => setSelectedId(null)} aria-label="关闭设备详情" className="absolute top-[9px] right-[9px] grid size-[27px] cursor-pointer place-items-center rounded-md bg-[#17212a] text-[#73818d] transition-colors hover:bg-[#22303a] hover:text-[#edf4f9]"><XIcon className="size-3.5" /></button>
          <Eyebrow>SELECTED ROOM</Eyebrow>
          <div className="mt-1 flex items-center gap-[11px]">
            <DeviceIcon ready={selectedDevice.ssh_available} partial={selectedDevice.frp_online} />
            <div className="min-w-0">
              <h2 className="mt-0 mb-1 truncate text-base text-[#edf4f9]" title={selectedDevice.name}>{selectedDevice.name}</h2>
              <code className="block truncate font-mono text-[9px] text-[#687985]" title={selectedDevice.hostname || selectedDevice.id}>{selectedDevice.hostname || selectedDevice.id}</code>
            </div>
          </div>
          <div className="mt-[15px] mb-[13px] grid grid-cols-4 gap-1.5 max-md:grid-cols-2">
            <span className="grid gap-1 rounded-[7px] border border-[#22313b] bg-[#0b1117] p-2"><small className="text-[8px] text-[#63727e]">FRP</small><strong className="truncate font-mono text-[10px] text-[#cdd7de]">{selectedDevice.frp_online ? '在线' : '离线'}</strong></span>
            <span className="grid gap-1 rounded-[7px] border border-[#22313b] bg-[#0b1117] p-2"><small className="text-[8px] text-[#63727e]">SSH</small><strong className="truncate font-mono text-[10px] text-[#cdd7de]">{selectedDevice.ssh_available ? '可用' : '不可用'}</strong></span>
            <span className="grid gap-1 rounded-[7px] border border-[#22313b] bg-[#0b1117] p-2"><small className="text-[8px] text-[#63727e]">终端</small><strong className="truncate font-mono text-[10px] text-[#cdd7de]">{selectedSessions.length}</strong></span>
            <span className="grid gap-1 rounded-[7px] border border-[#22313b] bg-[#0b1117] p-2"><small className="text-[8px] text-[#63727e]">端口</small><strong className="truncate font-mono text-[10px] text-[#cdd7de]">{selectedDevice.remote_port}</strong></span>
          </div>
          <div className="flex gap-[7px] max-md:flex-wrap">
            {selectedRecentSession && (
              <Button
                size="sm"
                className="h-auto px-3 py-[9px] text-[10px] font-bold"
                onClick={() => onSelectTerminal(selectedRecentSession.id)}
              >
                继续最近终端
              </Button>
            )}
            <Button variant={selectedSessions.length > 0 ? 'outline' : 'default'} size="sm" className="h-auto px-3 py-[9px] text-[10px] font-bold" disabled={busyId === selectedDevice.id || !selectedDevice.ssh_available} onClick={() => onOpen(selectedDevice)}>新建终端</Button>
            <Button variant="outline" size="sm" className="h-auto px-3 py-[9px] text-[10px]" onClick={() => onProbe(selectedDevice)}>检测</Button>
            <Button variant="outline" size="sm" className="h-auto px-3 py-[9px] text-[10px]" onClick={() => onEdit(selectedDevice)}>编辑</Button>
          </div>
        </aside>
      )}
    </section>
  )
}
