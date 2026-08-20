// Speech-bubble status popups for room characters.
//
// A bubble pops when a character's execution state *changes* — i.e. an agent
// hook, provider event or reporter fact landed and moved the projection.
// Transient states fade out after a few seconds; a run waiting on a person
// (approval, input, …) keeps its bubble until the wait resolves. The first
// sight also shows a meaningful active state so a room never appears silent
// while work is already in progress.

import type { RunActivity, RunLifecycle, WaitReason } from '../execution-state'

export type BubbleTone = 'think' | 'work' | 'test' | 'wait' | 'ok' | 'bad' | 'mute'

export interface BubbleState {
  lifecycle: RunLifecycle | null
  activity: RunActivity | null
  waitReason: WaitReason | null
  stale: boolean
  /** A durable execution event can be more specific than the current run state. */
  eventKey?: string | null
  eventText?: string | null
  eventTone?: BubbleTone
}

export interface Bubble {
  key: string
  text: string
  tone: BubbleTone
  eventKey?: string | null
  /** rAF clock seconds when the bubble popped; -Infinity means "no bubble". */
  shownAt: number
}

export const BUBBLE_TTL = 4.5
export const BUBBLE_TTL_OUTCOME = 6
export const BUBBLE_FADE = 0.8
export const BUBBLE_POP = 0.18

export const BUBBLE_TONE_COLOR: Record<BubbleTone, string> = {
  think: '#c9a9f4',
  work: '#77f2b4',
  test: '#91cce8',
  wait: '#e9bd68',
  ok: '#8affd2',
  bad: '#ff6b7a',
  mute: '#8a99a8',
}

const ACTIVITY_BUBBLE_TEXT: Partial<Record<RunActivity, string>> = {
  thinking: '思考中…',
  planning: '规划中…',
  coding: '写代码…',
  tooling: '调用工具…',
  testing: '跑测试…',
  reviewing: '审查中…',
  finalizing: '整理结果…',
}

const ACTIVITY_BUBBLE_TONE: Partial<Record<RunActivity, BubbleTone>> = {
  thinking: 'think',
  planning: 'think',
  reviewing: 'think',
  coding: 'work',
  tooling: 'work',
  testing: 'test',
  finalizing: 'mute',
}

const WAIT_BUBBLE_TEXT: Record<WaitReason, string> = {
  user_input: '等待输入',
  approval: '等待批准',
  authentication: '等待认证',
  tool: '等待工具',
  child_run: '等待子任务',
  network: '等待网络',
  rate_limit: '等待限流',
  retry_backoff: '等待重试',
  dependency: '等待依赖',
  resource: '等待资源',
  unknown: '等待中',
}

export function bubbleKey(state: BubbleState): string {
  return [
    state.lifecycle ?? '',
    state.activity ?? '',
    state.waitReason ?? '',
    state.stale ? 'stale' : '',
    state.eventKey ?? '',
    state.eventText ?? '',
  ].join('|')
}

/** Short status line for a state, or null when there is nothing worth saying. */
export function bubbleContent(state: BubbleState): { text: string; tone: BubbleTone } | null {
  if (state.eventText) {
    return { text: state.eventText, tone: state.eventTone ?? 'mute' }
  }
  switch (state.lifecycle) {
    case 'succeeded':
      return { text: '已完成', tone: 'ok' }
    case 'failed':
      return { text: '出错了', tone: 'bad' }
    case 'cancelled':
      return { text: '已取消', tone: 'mute' }
    case 'lost':
      return { text: '连接丢失', tone: 'bad' }
    default:
      break
  }
  if (state.stale) return { text: '状态过期…', tone: 'mute' }
  if (state.activity === 'waiting') {
    return {
      text: WAIT_BUBBLE_TEXT[state.waitReason ?? 'unknown'],
      tone: 'wait',
    }
  }
  const text = state.activity ? ACTIVITY_BUBBLE_TEXT[state.activity] : undefined
  if (!text || !state.activity) return null
  return { text, tone: ACTIVITY_BUBBLE_TONE[state.activity] ?? 'mute' }
}

/** A bubble stays pinned while its state still needs a person to act. */
export function bubblePinned(state: BubbleState): boolean {
  return !state.stale && state.lifecycle === 'running' && state.activity === 'waiting'
}

/**
 * Per-session bubble memory. Lives across canvas rebuilds (the scene is
 * recreated on every execution event), so a state change is detected by
 * comparing keys frame over frame.
 */
export class BubbleTracker {
  private bubbles = new Map<string, Bubble>()

  /** Advance one character; returns the bubble to consider drawing, if any. */
  track(sessionId: string, state: BubbleState, now: number): Bubble | null {
    const key = bubbleKey(state)
    const current = this.bubbles.get(sessionId)
    if (!current) {
      const content = bubbleContent(state)
      const initial: Bubble = {
        key,
        text: content?.text ?? '',
        tone: content?.tone ?? 'mute',
        eventKey: state.eventKey,
        shownAt: content ? now : Number.NEGATIVE_INFINITY,
      }
      this.bubbles.set(sessionId, initial)
      return content ? initial : null
    }
    if (current.key === key) return current.text ? current : null
    const eventChanged = Boolean(state.eventKey) && state.eventKey !== current.eventKey
    const content = bubbleContent(eventChanged
      ? state
      : { ...state, eventText: null, eventTone: undefined })
    const next: Bubble = content
      ? { key, text: content.text, tone: content.tone, eventKey: state.eventKey, shownAt: now }
      : { key, text: '', tone: 'mute', eventKey: state.eventKey, shownAt: Number.NEGATIVE_INFINITY }
    this.bubbles.set(sessionId, next)
    return content ? next : null
  }

  /** Drop sessions that no longer have a character in the scene. */
  prune(activeSessionIds: ReadonlySet<string>): void {
    for (const sessionId of this.bubbles.keys()) {
      if (!activeSessionIds.has(sessionId)) this.bubbles.delete(sessionId)
    }
  }
}

/** Opacity for the current frame; 0 hides the bubble. */
export function bubbleAlpha(bubble: Bubble, state: BubbleState, now: number): number {
  if (!bubble.text) return 0
  if (bubblePinned(state) && bubble.key === bubbleKey(state)) return 1
  const ttl = bubble.tone === 'ok' || bubble.tone === 'bad' ? BUBBLE_TTL_OUTCOME : BUBBLE_TTL
  const age = now - bubble.shownAt
  if (age >= ttl) return 0
  if (age >= ttl - BUBBLE_FADE) return (ttl - age) / BUBBLE_FADE
  return 1
}

/** Pop-in progress 0→1 over BUBBLE_POP seconds; 1 when reduced motion is on. */
export function bubblePop(bubble: Bubble, now: number, reduceMotion: boolean): number {
  if (reduceMotion) return 1
  return Math.min(1, Math.max(0, (now - bubble.shownAt) / BUBBLE_POP))
}

export interface BubbleDrawOptions {
  /** Counter-scale factor (1 / camera.scale) keeping the bubble screen-sized. */
  unit: number
  /** Room-interior horizontal clamp, in world coordinates. */
  clampX0: number
  clampX1: number
  pop: number
}

/**
 * Draw one pixel-styled speech bubble above a character's head.
 * (anchorX, headY) is the head-top center in world coordinates.
 */
export function drawBubble(
  context: CanvasRenderingContext2D,
  anchorX: number,
  headY: number,
  bubble: Bubble,
  alpha: number,
  options: BubbleDrawOptions,
): void {
  if (alpha <= 0 || !bubble.text) return
  const { unit, clampX0, clampX1, pop } = options
  const color = BUBBLE_TONE_COLOR[bubble.tone]
  const fontSize = 8 * unit
  const padX = 5 * unit
  const height = 14 * unit
  const tail = 3 * unit

  context.save()
  context.font = `700 ${fontSize}px Manrope, system-ui, sans-serif`
  const textWidth = context.measureText(bubble.text).width
  const width = Math.ceil(textWidth + padX * 2)

  const rise = (1 - pop) * 3 * unit
  const centerX = Math.min(Math.max(anchorX, clampX0 + width / 2), clampX1 - width / 2)
  const left = Math.round(centerX - width / 2)
  const top = Math.round(headY - 4 * unit - tail - height + rise)

  context.globalAlpha = alpha * (0.4 + 0.6 * pop)
  // tail first so the plaque overlaps its seam
  context.fillStyle = 'rgba(10,16,22,.94)'
  context.beginPath()
  context.moveTo(anchorX - tail, top + height - 1)
  context.lineTo(anchorX + tail, top + height - 1)
  context.lineTo(anchorX, top + height - 1 + tail + unit)
  context.closePath()
  context.fill()
  // plaque
  context.fillRect(left, top, width, height)
  context.strokeStyle = color
  context.lineWidth = unit
  context.strokeRect(left + unit / 2, top + unit / 2, width - unit, height - unit)
  // status text
  context.fillStyle = color
  context.textAlign = 'center'
  context.textBaseline = 'middle'
  context.fillText(bubble.text, left + width / 2, top + height / 2 + unit / 2)
  context.restore()
}
