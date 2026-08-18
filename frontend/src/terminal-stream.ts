const textEncoder = new TextEncoder()

export const MAX_RECOVERY_INPUT_BYTES = 64 * 1024

/**
 * xterm answers terminal capability/status queries through the same `onData`
 * event used by a person typing. Historical queries encountered while replaying
 * scrollback must not be sent to the live PTY after recovery.
 */
export function isSnapshotProtocolReply(data: string): boolean {
  if (/^\x1b\[(?:[?>])?\d+(?:;\d+)*(?:c|n|R|t|\$y)$/.test(data)) return true
  if (/^\x1b\][\s\S]*(?:\x07|\x1b\\)$/.test(data)) return true
  return /^\x1bP[\s\S]*\x1b\\$/.test(data)
}

/** Bounded user input accumulated while an initial snapshot is being parsed. */
export class RecoveryInputBuffer {
  private chunks: Uint8Array<ArrayBuffer>[] = []
  private size = 0

  constructor(private readonly maxBytes = MAX_RECOVERY_INPUT_BYTES) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
      throw new Error('recovery input limit must be a positive integer')
    }
  }

  get byteLength(): number {
    return this.size
  }

  push(data: string): boolean {
    const encoded = textEncoder.encode(data)
    if (this.size + encoded.byteLength > this.maxBytes) return false
    this.chunks.push(encoded)
    this.size += encoded.byteLength
    return true
  }

  drain(): Uint8Array<ArrayBuffer>[] {
    const chunks = this.chunks
    this.chunks = []
    this.size = 0
    return chunks
  }

  clear(): void {
    this.chunks = []
    this.size = 0
  }
}
