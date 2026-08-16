import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'

import {
  ApiError,
  api,
  type WorkspaceEntry,
  type WorkspaceListing,
} from '@/api'

export type DirectoryLoadState = {
  children: string[]
  status: 'idle' | 'loading' | 'loaded' | 'error'
  error: string
  revision: string | null
  nextCursor: string | null
  requestId: number
}

export type WorkspaceTreeRow =
  | { type: 'entry'; path: string; depth: number }
  | { type: 'more'; path: string; depth: number }

type TreeState = {
  workspaceId: string | null
  listing: WorkspaceListing | null
  nodes: Record<string, WorkspaceEntry>
  directories: Record<string, DirectoryLoadState>
  expanded: Record<string, true>
  selectedPath: string | null
}

type TreeAction =
  | { type: 'reset'; expanded: Record<string, true> }
  | { type: 'loadStart'; path: string; requestId: number; append: boolean }
  | { type: 'loadSuccess'; path: string; requestId: number; append: boolean; listing: WorkspaceListing }
  | { type: 'loadError'; path: string; requestId: number; error: string }
  | { type: 'toggle'; path: string }
  | { type: 'select'; path: string | null }
  | { type: 'invalidate'; paths: string[] }

const emptyDirectory = (): DirectoryLoadState => ({
  children: [],
  status: 'idle',
  error: '',
  revision: null,
  nextCursor: null,
  requestId: 0,
})

const initialState: TreeState = {
  workspaceId: null,
  listing: null,
  nodes: {},
  directories: { '': emptyDirectory() },
  expanded: {},
  selectedPath: null,
}

function normalizePath(path: string): string {
  return path === '.' ? '' : path.replaceAll('\\', '/').replace(/^\/+|\/+$/g, '')
}

export function parentWorkspacePath(path: string): string {
  const normalized = normalizePath(path)
  const index = normalized.lastIndexOf('/')
  return index < 0 ? '' : normalized.slice(0, index)
}

function reducer(state: TreeState, action: TreeAction): TreeState {
  if (action.type === 'reset') {
    return {
      ...initialState,
      directories: { '': emptyDirectory() },
      expanded: action.expanded,
    }
  }
  if (action.type === 'loadStart') {
    const current = state.directories[action.path] || emptyDirectory()
    return {
      ...state,
      directories: {
        ...state.directories,
        [action.path]: {
          ...current,
          status: 'loading',
          error: '',
          requestId: action.requestId,
          ...(!action.append ? { nextCursor: null } : {}),
        },
      },
    }
  }
  if (action.type === 'loadSuccess') {
    const current = state.directories[action.path] || emptyDirectory()
    if (current.requestId !== action.requestId) return state
    const workspaceChanged = Boolean(state.workspaceId && state.workspaceId !== action.listing.workspace_id)
    const baseNodes = workspaceChanged ? {} : state.nodes
    const baseDirectories = workspaceChanged ? { '': emptyDirectory() } : state.directories
    const baseExpanded = workspaceChanged ? {} : state.expanded
    const nextNodes = { ...baseNodes }
    for (const entry of action.listing.entries) nextNodes[normalizePath(entry.path)] = entry
    const incoming = action.listing.entries.map((entry) => normalizePath(entry.path))
    const children = action.append
      ? [...new Set([...current.children, ...incoming])]
      : incoming
    return {
      ...state,
      workspaceId: action.listing.workspace_id,
      listing: workspaceChanged || action.path === '' || !state.listing ? action.listing : state.listing,
      nodes: nextNodes,
      expanded: baseExpanded,
      directories: {
        ...baseDirectories,
        [action.path]: {
          children,
          status: 'loaded',
          error: '',
          revision: action.listing.revision,
          nextCursor: action.listing.next_cursor,
          requestId: action.requestId,
        },
      },
    }
  }
  if (action.type === 'loadError') {
    const current = state.directories[action.path] || emptyDirectory()
    if (current.requestId !== action.requestId) return state
    return {
      ...state,
      directories: {
        ...state.directories,
        [action.path]: { ...current, status: 'error', error: action.error },
      },
    }
  }
  if (action.type === 'toggle') {
    const expanded = { ...state.expanded }
    if (expanded[action.path]) delete expanded[action.path]
    else expanded[action.path] = true
    return { ...state, expanded }
  }
  if (action.type === 'select') return { ...state, selectedPath: action.path }
  if (action.type === 'invalidate') {
    const directories = { ...state.directories }
    for (const path of action.paths) {
      const current = directories[path]
      if (current) directories[path] = { ...current, status: 'idle', revision: null, nextCursor: null }
    }
    return { ...state, directories }
  }
  return state
}

function restoredExpanded(sessionId: string): Record<string, true> {
  try {
    const raw = window.sessionStorage.getItem(`agentserver:workspace-tree:${sessionId}`)
    const values = raw ? JSON.parse(raw) as unknown : []
    if (!Array.isArray(values)) return {}
    return Object.fromEntries(
      values.filter((value): value is string => typeof value === 'string').map((path) => [normalizePath(path), true]),
    )
  } catch {
    return {}
  }
}

export function useWorkspaceTree(sessionId: string) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const stateRef = useRef(state)
  const requestSequence = useRef(0)
  const controllers = useRef(new Map<string, AbortController>())
  stateRef.current = state

  const loadDirectory = useCallback(async (
    rawPath: string,
    options: { append?: boolean; force?: boolean } = {},
  ) => {
    const path = normalizePath(rawPath)
    const current = stateRef.current.directories[path] || emptyDirectory()
    if (!options.append && !options.force && (current.status === 'loading' || current.status === 'loaded')) return
    const append = Boolean(options.append && current.nextCursor)
    controllers.current.get(path)?.abort()
    const controller = new AbortController()
    controllers.current.set(path, controller)
    const requestId = ++requestSequence.current
    dispatch({ type: 'loadStart', path, requestId, append })
    try {
      const listing = await api.workspace(sessionId, path, {
        cursor: append ? current.nextCursor : null,
        revision: append ? current.revision : null,
        limit: 200,
        signal: controller.signal,
      })
      dispatch({ type: 'loadSuccess', path, requestId, append, listing })
    } catch (reason) {
      if (controller.signal.aborted) return
      if (append && reason instanceof ApiError && reason.code === 'WORKSPACE_FILE_CHANGED') {
        void loadDirectory(path, { force: true })
        return
      }
      dispatch({
        type: 'loadError',
        path,
        requestId,
        error: reason instanceof Error ? reason.message : '无法读取目录',
      })
    } finally {
      if (controllers.current.get(path) === controller) controllers.current.delete(path)
    }
  }, [sessionId])

  useEffect(() => {
    for (const controller of controllers.current.values()) controller.abort()
    controllers.current.clear()
    dispatch({ type: 'reset', expanded: restoredExpanded(sessionId) })
    void loadDirectory('', { force: true })
    return () => {
      for (const controller of controllers.current.values()) controller.abort()
      controllers.current.clear()
    }
  }, [loadDirectory, sessionId])

  useEffect(() => {
    try {
      window.sessionStorage.setItem(
        `agentserver:workspace-tree:${sessionId}`,
        JSON.stringify(Object.keys(state.expanded)),
      )
    } catch {
      // Storage can be disabled; tree navigation still works for this mount.
    }
  }, [sessionId, state.expanded])

  useEffect(() => {
    for (const path of Object.keys(state.expanded)) {
      const node = state.nodes[path]
      const directory = state.directories[path]
      if (node?.kind === 'directory' && (!directory || directory.status === 'idle')) {
        void loadDirectory(path)
      }
    }
  }, [loadDirectory, state.directories, state.expanded, state.nodes])

  const toggleDirectory = useCallback((rawPath: string) => {
    const path = normalizePath(rawPath)
    const expanded = Boolean(stateRef.current.expanded[path])
    dispatch({ type: 'toggle', path })
    const directory = stateRef.current.directories[path]
    if (!expanded && (!directory || directory.status === 'idle' || directory.status === 'error')) {
      void loadDirectory(path)
    }
  }, [loadDirectory])

  const refreshDirectory = useCallback((rawPath = '') => {
    void loadDirectory(normalizePath(rawPath), { force: true })
  }, [loadDirectory])

  const loadMore = useCallback((rawPath: string) => {
    void loadDirectory(normalizePath(rawPath), { append: true })
  }, [loadDirectory])

  const selectPath = useCallback((path: string | null) => {
    dispatch({ type: 'select', path: path === null ? null : normalizePath(path) })
  }, [])

  const invalidatePaths = useCallback((changedPaths: string[]) => {
    const targets = new Set<string>()
    for (const rawPath of changedPaths) {
      const path = normalizePath(rawPath)
      if (stateRef.current.directories[path]) targets.add(path)
      else targets.add(parentWorkspacePath(path))
    }
    const paths = [...targets]
    dispatch({ type: 'invalidate', paths })
    for (const path of paths) {
      if (path === '' || stateRef.current.expanded[path]) void loadDirectory(path, { force: true })
    }
  }, [loadDirectory])

  const rows = useMemo(() => {
    const result: WorkspaceTreeRow[] = []
    const visit = (directoryPath: string, depth: number) => {
      const directory = state.directories[directoryPath]
      for (const path of directory?.children || []) {
        result.push({ type: 'entry', path, depth })
        if (state.nodes[path]?.kind === 'directory' && state.expanded[path]) visit(path, depth + 1)
      }
      if (directory?.nextCursor) result.push({ type: 'more', path: directoryPath, depth })
    }
    visit('', 0)
    return result
  }, [state.directories, state.expanded, state.nodes])

  const watchedPaths = useMemo(() => {
    const paths = new Set<string>([''])
    for (const path of Object.keys(state.expanded)) paths.add(path)
    if (state.selectedPath) paths.add(state.selectedPath)
    return [...paths].slice(0, 64)
  }, [state.expanded, state.selectedPath])

  return {
    ...state,
    rows,
    watchedPaths,
    loadDirectory,
    loadMore,
    refreshDirectory,
    toggleDirectory,
    selectPath,
    invalidatePaths,
  }
}
