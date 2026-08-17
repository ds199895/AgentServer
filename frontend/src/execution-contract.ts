export function isExecutionUnavailableStatus(status: number | undefined): boolean {
  return status === 404 || status === 501
}

export function shouldReconnectExecutionSocket(code: number): boolean {
  return code !== 4401
}

export function executionSocketPath(afterSequence: number): string {
  const cursor = Number.isFinite(afterSequence) ? Math.max(0, Math.floor(afterSequence)) : 0
  return `/ws/execution?after_sequence=${cursor}`
}
