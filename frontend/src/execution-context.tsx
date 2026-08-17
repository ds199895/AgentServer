import { createContext, useContext, type ReactNode } from 'react'

import type { ExecutionStreamState } from '@/useExecutionStream'

const EMPTY_EXECUTION: ExecutionStreamState = {
  snapshot: null,
  status: 'disabled',
  error: '',
  available: false,
  freshness_now: Date.now(),
  refresh: () => undefined,
}

const ExecutionContext = createContext<ExecutionStreamState>(EMPTY_EXECUTION)

export function ExecutionProvider({
  value,
  children,
}: {
  value: ExecutionStreamState
  children: ReactNode
}) {
  return <ExecutionContext.Provider value={value}>{children}</ExecutionContext.Provider>
}

export function useExecutionContext(): ExecutionStreamState {
  return useContext(ExecutionContext)
}
