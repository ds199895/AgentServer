// Pure scene-layout model for the pixel device world.
// All variety is derived from hash(device.id) so re-renders never jump around.

import type { Device, TerminalSession } from '../api'
import type { RunActivity, RunLifecycle, WaitReason } from '../execution-state'
import { hashString, type StatusKind } from './sprites'

export const TILE = 16
export const ROOM_W = 224 // 14 tiles
export const ROOM_H = 160 // 10 tiles
export const WALL = 8
export const PAD = 56

export type DecorKind = 'lamp' | 'shelf' | 'ac'

export interface CharSlot {
  sessionId: string
  active: boolean
  pose: 'sit' | 'stand'
  x: number // relative to room origin
  y: number
  variant: number
  /** Detected agent kind (AGENT_OUTFITS key), null for plain shell sessions. */
  agent: string | null
  lifecycle: RunLifecycle | null
  activity: RunActivity | null
  waitReason: WaitReason | null
  stale: boolean
  agentCwd: string
  runLabel: string | null
  eventKey: string | null
  eventText: string | null
  eventTone: 'think' | 'work' | 'test' | 'wait' | 'ok' | 'bad' | 'mute' | undefined
}

export type TerminalExecutionVisual = {
  agentKind: string | null
  lifecycle: RunLifecycle | null
  activity: RunActivity | null
  waitReason: WaitReason | null
  stale: boolean
  agentCwd: string
  runLabel: string | null
  eventKey: string | null
  eventText: string | null
  eventTone: 'think' | 'work' | 'test' | 'wait' | 'ok' | 'bad' | 'mute' | undefined
}

export interface RoomModel {
  device: Device
  status: StatusKind
  x: number
  y: number
  floorVariant: number
  deskLeft: boolean
  metalDesk: boolean
  windowVariant: number
  posterVariant: number
  plantVariant: number
  decorKind: DecorKind
  chars: CharSlot[]
}

export interface DecorItem {
  kind: 'crate' | 'pipe'
  x: number
  y: number
}

export interface SceneModel {
  rooms: RoomModel[]
  decor: DecorItem[]
  worldW: number
  worldH: number
}

export function deviceStatus(device: Device): StatusKind {
  if (device.ssh_available) return 'ready'
  if (device.frp_online) return 'partial'
  return 'offline'
}

// Standing slots on the rug (relative to room origin), in fill order.
// Spaced ~20px apart for the 16×22 sprites; all stay on the 64×40 rug.
const STAND_SLOTS = [
  { x: 82, y: 96 },
  { x: 102, y: 96 },
  { x: 122, y: 96 },
  { x: 82, y: 114 },
  { x: 102, y: 114 },
  { x: 122, y: 114 },
  { x: 92, y: 105 },
]

export function buildScene(
  devices: Device[],
  sessions: TerminalSession[],
  aspect: number,
  executionByTerminal: ReadonlyMap<string, TerminalExecutionVisual> = new Map(),
): SceneModel {
  const count = devices.length
  const safeAspect = Math.min(2.4, Math.max(0.6, aspect || 1.6))
  let columns = Math.min(5, Math.max(1, Math.ceil(Math.sqrt(count * safeAspect))))
  // rebalance so the last row is as full as possible (6 rooms: 4+2 → 3+3)
  if (count > 1) columns = Math.ceil(count / Math.ceil(count / columns))

  const sessionsByDevice = new Map<string, TerminalSession[]>()
  sessions.forEach((session) => {
    if (!session.device_id) return
    const list = sessionsByDevice.get(session.device_id) || []
    list.push(session)
    sessionsByDevice.set(session.device_id, list)
  })

  const rooms: RoomModel[] = devices.map((device, index) => {
    const hash = hashString(device.id)
    const column = index % columns
    const row = Math.floor(index / columns)
    // rooms are contiguous: neighbors share a single wall (pitch = size - WALL)
    const x = PAD + column * (ROOM_W - WALL)
    const y = PAD + row * (ROOM_H - WALL)
    const deskLeft = (hash & 1) === 0
    const deskX = deskLeft ? 20 : 156

    const deviceSessions = sessionsByDevice.get(device.id) || []
    const chars: CharSlot[] = deviceSessions.slice(0, STAND_SLOTS.length + 1).map((session, slot) => {
      const variant = hashString(session.id)
      const execution = executionByTerminal.get(session.id)
      const agent = execution?.agentKind ?? session.agent?.kind ?? null
      const runState = {
        lifecycle: execution?.lifecycle ?? null,
        activity: execution?.activity ?? null,
        waitReason: execution?.waitReason ?? null,
        stale: execution?.stale ?? false,
        agentCwd: execution?.agentCwd ?? session.agent?.cwd ?? '',
        runLabel: execution?.runLabel ?? null,
        eventKey: execution?.eventKey ?? null,
        eventText: execution?.eventText ?? null,
        eventTone: execution?.eventTone,
      }
      if (slot === 0) {
        // Seated at the desk, back to the viewer; the sprite includes the
        // chair and is anchored so the hands land on the keyboard (desk-local
        // x 18-30, y 26-31) and the feet stay clear of the y+64 nameplate.
        return { sessionId: session.id, active: session.active, pose: 'sit', x: deskX + 15, y: 28, variant, agent, ...runState }
      }
      const spot = STAND_SLOTS[(slot - 1) % STAND_SLOTS.length]
      return { sessionId: session.id, active: session.active, pose: 'stand', x: spot.x, y: spot.y, variant, agent, ...runState }
    })

    const decorRoll = (hash >>> 3) % 3
    return {
      device,
      status: deviceStatus(device),
      x,
      y,
      floorVariant: hash % 3,
      deskLeft,
      metalDesk: (hash & 2) !== 0,
      windowVariant: (hash >>> 2) % 2,
      posterVariant: (hash >>> 5) % 2,
      plantVariant: (hash >>> 4) % 2,
      decorKind: decorRoll === 0 ? 'lamp' : decorRoll === 1 ? 'shelf' : 'ac',
      chars,
    }
  })

  const rows = Math.ceil(count / columns)
  const worldW = count ? PAD * 2 + columns * (ROOM_W - WALL) + WALL : 0
  const worldH = count ? PAD * 2 + rows * (ROOM_H - WALL) + WALL : 0

  // Props on the apron around the building (rooms share walls, no corridors).
  const decor: DecorItem[] = []
  if (count) {
    const decorCount = Math.min(5, Math.max(2, count))
    for (let item = 0; item < decorCount; item += 1) {
      const hash = hashString(`apron:${item}`)
      decor.push({
        kind: hash % 3 === 0 ? 'pipe' : 'crate',
        x: Math.min(worldW - PAD - 24, PAD + 20 + item * 56 + (hash % 14)),
        y: worldH - PAD + 16 + (hash % 8),
      })
    }
  }

  return { rooms, decor, worldW, worldH }
}

// ------------------------------------------------------------- room layout
// Concrete pixel positions (relative to room origin) for every fixture.
// Rules keep furniture clear of each other for any variant combination.

export interface RoomLayout {
  deskX: number
  deskY: number
  monitorX: number
  monitorY: number
  chairX: number
  chairY: number
  rackX: number
  rackY: number
  plantX: number
  plantY: number
  lampX: number | null
  shelfX: number | null
  windowX: number | null
  acX: number | null
  posterX: number
}

export function roomLayout(room: RoomModel): RoomLayout {
  const deskX = room.deskLeft ? 20 : 156
  return {
    deskX,
    deskY: 12,
    monitorX: deskX + 16,
    monitorY: 6,
    chairX: deskX + 18,
    chairY: 40,
    rackX: room.deskLeft ? 190 : 18,
    rackY: 16,
    plantX: 16,
    plantY: 122,
    lampX: room.decorKind === 'lamp' ? 196 : null,
    shelfX: room.decorKind === 'shelf' ? (room.deskLeft ? 78 : 122) : null,
    windowX: room.decorKind === 'ac' ? null : 100,
    acX: room.decorKind === 'ac' ? 102 : null,
    posterX: room.deskLeft ? 74 : 138,
  }
}
