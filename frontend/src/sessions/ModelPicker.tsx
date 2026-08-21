import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Cpu } from 'lucide-react'

/**
 * Per-turn model and reasoning-effort override.
 *
 * The catalogue comes from the provider itself (Codex answers `model/list`
 * over its app-server connection), so this offers what the session can
 * actually run rather than a hardcoded list. An override is sent only when the
 * user picks one; an untouched picker leaves the provider default alone.
 */

export type AgentModel = {
  id: string
  name: string
  isDefault: boolean
  /** Reasoning efforts this model accepts; empty means the provider decides. */
  efforts: string[]
}

export type ModelSelection = {
  model: string | null
  effort: string | null
}

const EFFORT_LABELS: Record<string, string> = {
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
}

function effortLabel(value: string): string {
  return EFFORT_LABELS[value] ?? value.charAt(0).toUpperCase() + value.slice(1)
}

type Props = {
  models: AgentModel[]
  sessionModel: string | null
  selection: ModelSelection
  onChange: (selection: ModelSelection) => void
  disabled?: boolean
}

export default function ModelPicker({
  models,
  sessionModel,
  selection,
  onChange,
  disabled,
}: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  // Close on outside click and Escape so the menu never strands itself open
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

  // Which model is actually in force, so effort options match what will run.
  const activeModel = useMemo(() => {
    const slug = selection.model ?? sessionModel
    return (
      models.find((model) => model.id === slug) ??
      models.find((model) => model.isDefault) ??
      null
    )
  }, [models, selection.model, sessionModel])

  const efforts = activeModel?.efforts ?? []
  const label = activeModel?.name ?? selection.model ?? sessionModel ?? 'Default model'

  function pickModel(model: AgentModel | null) {
    // Effort is a property of the model; dropping an unsupported one avoids
    // sending a combination the provider would reject.
    const nextEffort =
      selection.effort && model && !model.efforts.includes(selection.effort)
        ? null
        : selection.effort
    onChange({ model: model?.id ?? null, effort: nextEffort })
    setOpen(false)
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        title="Model and reasoning effort for the next turn"
        className="flex max-w-[240px] items-center gap-1.5 rounded-md border border-border px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Cpu size={13} className="shrink-0" />
        <span className="truncate">{label}</span>
        {selection.effort && (
          <span className="shrink-0 rounded bg-accent px-1 text-[10px]">
            {effortLabel(selection.effort)}
          </span>
        )}
        <ChevronDown size={12} className="shrink-0" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1 w-72 overflow-hidden rounded-lg border border-border bg-popover shadow-lg">
          <div className="max-h-72 overflow-y-auto">
            <div className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              Model
            </div>

            {models.length === 0 && (
              <div className="px-3 pb-2 text-[11px] text-muted-foreground">
                This provider did not report a model catalogue. The next turn
                uses {sessionModel ? `“${sessionModel}”` : 'the provider default'}.
              </div>
            )}

            {models.length > 0 && (
              <button
                type="button"
                onClick={() => pickModel(null)}
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
            )}

            {models.map((model) => (
              <button
                key={model.id}
                type="button"
                onClick={() => pickModel(model)}
                className="flex w-full items-start gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
              >
                <Check
                  size={13}
                  className={`mt-0.5 shrink-0 ${
                    selection.model === model.id ? 'text-primary' : 'invisible'
                  }`}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-foreground">{model.name}</span>
                  {model.name !== model.id && (
                    <span className="block truncate font-mono text-[10px] text-muted-foreground">
                      {model.id}
                    </span>
                  )}
                </span>
                {model.isDefault && (
                  <span className="mt-0.5 shrink-0 rounded bg-accent px-1 text-[9px] text-muted-foreground">
                    default
                  </span>
                )}
              </button>
            ))}

            {efforts.length > 0 && (
              <>
                <div className="mt-1 border-t border-border px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  Reasoning effort
                </div>
                <button
                  type="button"
                  onClick={() => {
                    onChange({ ...selection, effort: null })
                    setOpen(false)
                  }}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
                >
                  <Check
                    size={13}
                    className={selection.effort === null ? 'text-primary' : 'invisible'}
                  />
                  <span>Default effort</span>
                </button>
                {efforts.map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => {
                      onChange({ ...selection, effort: value })
                      setOpen(false)
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent"
                  >
                    <Check
                      size={13}
                      className={selection.effort === value ? 'text-primary' : 'invisible'}
                    />
                    <span>{effortLabel(value)}</span>
                  </button>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
