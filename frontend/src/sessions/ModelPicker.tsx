import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Cpu } from 'lucide-react'

/**
 * Per-turn model and reasoning-effort override.
 *
 * The session carries a default model chosen at creation; this only sends an
 * override when the user picks something else, so an untouched picker leaves
 * the provider's own default alone. Options come from the device's advertised
 * provider capabilities where available, because the set of installed models
 * is a property of the device, not of this UI.
 */

export type ModelSelection = {
  model: string | null
  effort: string | null
}

const EFFORT_OPTIONS = [
  { value: null, label: 'Default effort' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

type Props = {
  /** Model names advertised by the device, if it reported any. */
  available: string[]
  sessionModel: string | null
  selection: ModelSelection
  onChange: (selection: ModelSelection) => void
  disabled?: boolean
}

export default function ModelPicker({
  available,
  sessionModel,
  selection,
  onChange,
  disabled,
}: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  // Close on outside click and Escape, so the menu never strands itself open
  // over the transcript.
  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const activeModel = selection.model ?? sessionModel
  const activeEffort = EFFORT_OPTIONS.find((item) => item.value === selection.effort)
  const label = activeModel || 'Default model'

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        title="Model and reasoning effort for the next turn"
        className="flex max-w-[220px] items-center gap-1.5 rounded-md border border-border px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Cpu size={13} className="shrink-0" />
        <span className="truncate">{label}</span>
        {selection.effort && (
          <span className="shrink-0 rounded bg-accent px-1 text-[10px]">{selection.effort}</span>
        )}
        <ChevronDown size={12} className="shrink-0" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1 w-64 overflow-hidden rounded-lg border border-border bg-popover shadow-lg">
          <div className="max-h-56 overflow-y-auto">
            <div className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              Model
            </div>
            <button
              type="button"
              onClick={() => {
                onChange({ ...selection, model: null })
                setOpen(false)
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
            >
              <Check
                size={13}
                className={selection.model === null ? 'text-primary' : 'invisible'}
              />
              <span className="truncate">
                {sessionModel ? `Session default (${sessionModel})` : 'Provider default'}
              </span>
            </button>
            {available.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => {
                  onChange({ ...selection, model: name })
                  setOpen(false)
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
              >
                <Check
                  size={13}
                  className={selection.model === name ? 'text-primary' : 'invisible'}
                />
                <span className="truncate">{name}</span>
              </button>
            ))}
            {available.length === 0 && (
              <div className="px-3 pb-1 text-[11px] text-muted-foreground">
                This device did not advertise a model list; the provider default is used.
              </div>
            )}

            <div className="mt-1 border-t border-border px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              Reasoning effort
            </div>
            {EFFORT_OPTIONS.map((item) => (
              <button
                key={item.label}
                type="button"
                onClick={() => {
                  onChange({ ...selection, effort: item.value })
                  setOpen(false)
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
              >
                <Check
                  size={13}
                  className={activeEffort?.value === item.value ? 'text-primary' : 'invisible'}
                />
                <span>{item.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
