import { useCallback, useEffect, useState } from 'react'
import { CornerLeftUp, Folder, LoaderCircle, RefreshCw } from 'lucide-react'
import { agentApi } from './api'

/**
 * Browse a device's directories to choose a session's working directory.
 *
 * The default `.` resolves to wherever the runtime host process happens to be
 * running — its install directory — which is never where the user wants an
 * agent to work. Listing is directory-only and comes from the device itself,
 * because the server has no view of a remote device's filesystem.
 */
export function DirectoryPicker({
  deviceId,
  value,
  onChange,
}: {
  deviceId: string
  value: string
  onChange: (path: string) => void
}) {
  const [entries, setEntries] = useState<Array<{ name: string; path: string }>>([])
  const [parent, setParent] = useState<string | null>(null)
  const [current, setCurrent] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [truncated, setTruncated] = useState(false)

  const load = useCallback(
    async (path: string | null) => {
      setLoading(true)
      setError('')
      try {
        const result = await agentApi.browse(deviceId, path)
        setEntries(result.entries)
        setParent(result.parent)
        setCurrent(result.path)
        setTruncated(result.truncated)
        onChange(result.path)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : 'Unable to list directories')
      } finally {
        setLoading(false)
      }
    },
    [deviceId, onChange],
  )

  // Start from the device's home directory rather than the host's cwd.
  useEffect(() => {
    void load(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId])

  return (
    <div className="rounded-md border border-input">
      <div className="flex items-center gap-1.5 border-b border-border px-2 py-1.5">
        <button
          type="button"
          onClick={() => parent && void load(parent)}
          disabled={!parent || loading}
          title="Parent directory"
          aria-label="Parent directory"
          className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
        >
          <CornerLeftUp size={14} />
        </button>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-foreground" title={current}>
          {current || '…'}
        </span>
        {loading ? (
          <LoaderCircle size={13} className="animate-spin text-muted-foreground" />
        ) : (
          <button
            type="button"
            onClick={() => void load(current || null)}
            title="Refresh"
            aria-label="Refresh directory listing"
            className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground"
          >
            <RefreshCw size={13} />
          </button>
        )}
      </div>

      <div className="max-h-44 overflow-y-auto">
        {error && <div className="px-2 py-2 text-xs text-destructive">{error}</div>}
        {!error && !loading && entries.length === 0 && (
          <div className="px-2 py-2 text-xs text-muted-foreground">No subdirectories here.</div>
        )}
        {entries.map((entry) => (
          <button
            key={entry.path}
            type="button"
            onClick={() => void load(entry.path)}
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs transition-colors hover:bg-accent"
          >
            <Folder size={13} className="shrink-0 text-muted-foreground" />
            <span className="truncate">{entry.name}</span>
          </button>
        ))}
        {truncated && (
          <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
            Showing the first 200 directories.
          </div>
        )}
      </div>

      <div className="border-t border-border px-2 py-1.5">
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="/path/to/project"
          className="w-full bg-transparent font-mono text-[11px] text-foreground outline-none"
          aria-label="Working directory"
        />
      </div>
    </div>
  )
}
