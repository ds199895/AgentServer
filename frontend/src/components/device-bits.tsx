import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export function DeviceIcon({ ready, partial, className }: { ready: boolean; partial: boolean; className?: string }) {
  return (
    <span
      className={cn(
        'grid size-[35px] shrink-0 place-items-center rounded-lg border bg-[#151c23] font-mono text-[10px] font-bold text-[#697581]',
        ready
          ? 'border-[#315a48] bg-[#14241e] text-primary'
          : partial
            ? 'border-[#655637] bg-[#251f14] text-[#e1ba66]'
            : 'border-[#303b46]',
        className,
      )}
    >
      &gt;_
    </span>
  )
}

export function StateBadge({ online, label }: { online: boolean; label: string }) {
  return (
    <Badge variant={online ? 'default' : 'secondary'}>
      <i
        className={cn(
          'size-[5px] rounded-full bg-[#d65f6c]',
          online && 'bg-primary shadow-[0_0_7px_#77f2b488]',
        )}
      />
      {label}
    </Badge>
  )
}
