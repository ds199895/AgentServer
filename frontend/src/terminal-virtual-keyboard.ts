export type VirtualModifier = 'shift' | 'ctrl' | 'alt'

export type VirtualKeyName =
  | 'escape'
  | 'arrowUp'
  | 'enter'
  | 'arrowLeft'
  | 'arrowDown'
  | 'arrowRight'
  | 'tab'
  | 'ctrlC'

export type VirtualModifierState = Record<VirtualModifier, boolean>

export const EMPTY_VIRTUAL_MODIFIERS: VirtualModifierState = {
  shift: false,
  ctrl: false,
  alt: false,
}

/**
 * Encode an accessory-keyboard key the same way xterm encodes a physical key.
 * Ctrl/Alt/Shift are latched by the UI and consumed by the next non-modifier key.
 */
export function encodeVirtualKey(
  key: VirtualKeyName,
  modifiers: VirtualModifierState,
  applicationCursorKeys = false,
): string {
  const modifierValue =
    (modifiers.shift ? 1 : 0) |
    (modifiers.alt ? 2 : 0) |
    (modifiers.ctrl ? 4 : 0)

  if (key === 'tab') return modifiers.shift ? '\x1b[Z' : '\t'
  // Ctrl+C 就是 ETX 控制字节,无需与其他修饰键组合。
  if (key === 'ctrlC') return '\x03'
  if (key === 'enter') return modifiers.alt ? '\x1b\r' : '\r'
  if (key === 'escape') return modifiers.alt ? '\x1b\x1b' : '\x1b'

  const final = {
    arrowUp: 'A',
    arrowDown: 'B',
    arrowRight: 'C',
    arrowLeft: 'D',
  }[key]

  if (modifierValue) return `\x1b[1;${modifierValue + 1}${final}`
  return applicationCursorKeys ? `\x1bO${final}` : `\x1b[${final}`
}
