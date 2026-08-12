import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p className={cn('m-0 mb-1.5 font-mono text-[10px] leading-none font-medium tracking-[0.2em] text-primary', className)}>
      {children}
    </p>
  )
}
