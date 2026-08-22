export type NativeInputState = {
  composing: boolean
  pending: boolean
}

type NativeInputBridgeOptions = {
  onFallbackText: (data: string) => void
  state?: NativeInputState
  schedule?: (callback: () => void) => number
  cancel?: (handle: number) => void
}

type PendingText = {
  data: string
  handle: number
}

const TEXT_INPUT_TYPES = new Set(['insertText', 'insertReplacementText', 'insertFromComposition'])
const RECENT_XTERM_WINDOW_MS = 32

/**
 * Observe native text commits that xterm 6 can miss on WebKit IMEs.
 *
 * xterm remains the primary input path. This bridge only forwards committed
 * InputEvent.data when no matching xterm onData event was observed. It must
 * never call terminal.input(), because that would re-enter xterm and duplicate
 * the native text event.
 */
export function attachNativeInputBridge(
  textarea: HTMLTextAreaElement,
  options: NativeInputBridgeOptions,
): {
  state: NativeInputState
  noteXtermData: (data: string) => void
  dispose: () => void
} {
  const state = options.state ?? { composing: false, pending: false }
  const schedule = options.schedule ?? ((callback: () => void) => window.setTimeout(callback, 16))
  const cancel = options.cancel ?? ((handle: number) => window.clearTimeout(handle))
  const pending: PendingText[] = []
  const recentXterm: Array<{ data: string; expiresAt: number }> = []
  let disposed = false

  const pruneRecent = () => {
    const now = Date.now()
    while (recentXterm.length > 0 && recentXterm[0].expiresAt < now) recentXterm.shift()
  }

  const consumeRecent = (data: string): boolean => {
    pruneRecent()
    const index = recentXterm.findIndex((entry) => entry.data === data)
    if (index < 0) return false
    recentXterm.splice(index, 1)
    return true
  }

  const updatePendingState = () => {
    state.pending = pending.length > 0
    textarea.dataset.nativeInputPending = state.pending ? 'true' : 'false'
  }

  const setComposing = (value: boolean) => {
    state.composing = value
    textarea.dataset.nativeComposing = value ? 'true' : 'false'
  }

  const removePending = (entry: PendingText) => {
    const index = pending.indexOf(entry)
    if (index >= 0) pending.splice(index, 1)
    cancel(entry.handle)
    updatePendingState()
  }

  const fallback = (entry: PendingText) => {
    if (disposed) return
    if (state.composing) {
      entry.handle = schedule(() => fallback(entry))
      return
    }
    const index = pending.indexOf(entry)
    if (index < 0) return
    pending.splice(index, 1)
    updatePendingState()
    if (entry.data) options.onFallbackText(entry.data)
  }

  const noteXtermData = (data: string) => {
    if (disposed || !data) return
    const index = pending.findIndex((entry) => entry.data === data)
    if (index >= 0) {
      removePending(pending[index])
      return
    }
    pruneRecent()
    recentXterm.push({ data, expiresAt: Date.now() + RECENT_XTERM_WINDOW_MS + 1 })
    if (recentXterm.length > 8) recentXterm.shift()
  }

  const onCompositionStart = () => {
    setComposing(true)
  }
  const onCompositionEnd = () => {
    setComposing(false)
  }
  const onCompositionCancel = () => {
    setComposing(false)
  }
  const onBeforeInput = (event: Event) => {
    const input = event as InputEvent
    if (!input.data || !TEXT_INPUT_TYPES.has(input.inputType)) return
    if (consumeRecent(input.data)) return
    const entry: PendingText = { data: input.data, handle: 0 }
    entry.handle = schedule(() => fallback(entry))
    pending.push(entry)
    updatePendingState()
  }
  const onInput = (event: Event) => {
    const input = event as InputEvent
    if (!input.data || !TEXT_INPUT_TYPES.has(input.inputType)) return
    // WebKit may expose only `input`, without `beforeinput`.
    if (pending.some((entry) => entry.data === input.data) || consumeRecent(input.data)) return
    const entry: PendingText = { data: input.data, handle: 0 }
    entry.handle = schedule(() => fallback(entry))
    pending.push(entry)
    updatePendingState()
  }
  const onBlur = () => {
    // A blur ends an abandoned composition on iOS. Committed xterm data has
    // already gone through noteXtermData and must not be replayed here.
    setComposing(false)
  }

  textarea.addEventListener('compositionstart', onCompositionStart)
  textarea.addEventListener('compositionend', onCompositionEnd)
  textarea.addEventListener('compositioncancel', onCompositionCancel)
  textarea.addEventListener('beforeinput', onBeforeInput)
  textarea.addEventListener('input', onInput)
  textarea.addEventListener('blur', onBlur)

  return {
    state,
    noteXtermData,
    dispose: () => {
      if (disposed) return
      disposed = true
      textarea.removeEventListener('compositionstart', onCompositionStart)
      textarea.removeEventListener('compositionend', onCompositionEnd)
      textarea.removeEventListener('compositioncancel', onCompositionCancel)
      textarea.removeEventListener('beforeinput', onBeforeInput)
      textarea.removeEventListener('input', onInput)
      textarea.removeEventListener('blur', onBlur)
      for (const entry of pending) cancel(entry.handle)
      pending.length = 0
      recentXterm.length = 0
      state.composing = false
      updatePendingState()
    },
  }
}

export function configureTerminalInputTextarea(textarea: HTMLTextAreaElement): void {
  textarea.autocapitalize = 'none'
  textarea.autocomplete = 'off'
  textarea.setAttribute('autocorrect', 'off')
  textarea.spellcheck = false
}
