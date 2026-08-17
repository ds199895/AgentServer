import { GitBranch } from 'lucide-react'

import { RunStatusBadge } from '@/components/RunStatusBadge'
import {
  runTreeRoots,
  runsByParent,
  type ExecutionRun,
  type ExecutionSnapshot,
} from '@/execution-state'
import { cn } from '@/lib/utils'

function RunNode({
  snapshot,
  run,
  selectedRunId,
  depth,
  path,
}: {
  snapshot: ExecutionSnapshot
  run: ExecutionRun
  selectedRunId: string
  depth: number
  path: ReadonlySet<string>
}) {
  const cyclic = path.has(run.run_id)
  const nextPath = new Set(path)
  nextPath.add(run.run_id)
  const directChildren = cyclic ? [] : runsByParent(snapshot, run.run_id)
  const truncated = !cyclic && depth >= 8 && directChildren.length > 0
  const children = truncated ? [] : directChildren
  const agent = run.agent_instance_id
    ? snapshot.agents.find((item) => item.agent_instance_id === run.agent_instance_id) ?? null
    : null
  return (
    <li className="relative grid gap-1.5 pl-4 before:absolute before:top-0 before:bottom-0 before:left-1 before:w-px before:bg-[#26333d] last:before:h-3">
      <div
        className={cn(
          'relative flex min-w-0 items-center gap-2 rounded-md border border-transparent px-1.5 py-1 before:absolute before:top-1/2 before:right-full before:w-3 before:border-t before:border-[#26333d]',
          run.run_id === selectedRunId && 'border-[#315a48] bg-[#122019]',
        )}
      >
        <div className="min-w-0 flex-1">
          <strong className="block truncate font-mono text-[9px] text-[#dce5eb]" title={run.summary || run.run_id}>
            {run.summary || `Run ${run.run_id.slice(0, 8)}`}
          </strong>
          <small className="font-mono text-[7px] text-[#62727e]">
            {run.run_id.slice(0, 8)}{run.attempt ? ` · attempt ${run.attempt}` : ''}
          </small>
        </div>
        <RunStatusBadge run={run} agent={agent} compact />
      </div>
      {cyclic && <small className="pl-2 text-[8px] text-[#ff9aa4]">检测到循环血缘，已停止展开</small>}
      {truncated && (
        <small className="pl-2 text-[8px] text-[#e9bd68]">层级过深，已停止展开</small>
      )}
      {children.length > 0 && (
        <ul className="grid gap-1.5">
          {children.map((child) => (
            <RunNode
              key={child.run_id}
              snapshot={snapshot}
              run={child}
              selectedRunId={selectedRunId}
              depth={depth + 1}
              path={nextPath}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

export function RunTree({ snapshot, run }: { snapshot: ExecutionSnapshot; run: ExecutionRun }) {
  const roots = runTreeRoots(snapshot, run)
  return (
    <section aria-label="父子 Agent 运行关系" className="grid gap-2.5">
      <header className="flex items-center gap-2 text-[10px] font-semibold text-[#aab8c2]">
        <GitBranch className="size-3.5 text-[#7bbd9e]" />
        父子运行关系
      </header>
      <ul className="grid gap-1.5">
        {roots.map((root) => (
          <RunNode
            key={root.run_id}
            snapshot={snapshot}
            run={root}
            selectedRunId={run.run_id}
            depth={0}
            path={new Set()}
          />
        ))}
      </ul>
    </section>
  )
}
