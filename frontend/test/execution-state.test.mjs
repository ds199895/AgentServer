import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { transformWithOxc } from 'vite'

const sourceUrl = new URL('../src/execution-state.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const transpiled = await transformWithOxc(source, sourceUrl.pathname, { lang: 'ts' })
const execution = await import(
  `data:text/javascript;base64,${Buffer.from(transpiled.code).toString('base64')}`
)

function evidence(source = 'reported', overrides = {}) {
  return { source, confidence: 0.95, ...overrides }
}

function run(overrides = {}) {
  return {
    run_id: 'run-1',
    owner_id: 'alice',
    task_id: 'task-1',
    assignment_id: 'assignment-1',
    terminal_id: 'terminal-1',
    lifecycle: 'running',
    activity: 'coding',
    wait_reason: null,
    revision: 1,
    view_sequence: 4,
    last_sequence: 4,
    updated_at: '2026-08-17T12:00:00Z',
    evidence: {
      lifecycle: evidence('control'),
      activity: evidence('reported'),
      progress: evidence('reported'),
    },
    ...overrides,
  }
}

function snapshot(overrides = {}) {
  return {
    schema: 'agentserver.execution-snapshot/1',
    as_of_sequence: 4,
    tasks: [],
    assignments: [],
    agents: [],
    runs: [run()],
    terminal_bindings: [{
      terminal_id: 'terminal-1',
      active_run_id: 'run-1',
      active_agent_instance_id: null,
      revision: 1,
      view_sequence: 4,
    }],
    ...overrides,
  }
}

function message(cursor, projection) {
  return {
    type: 'event',
    cursor,
    event: { global_sequence: cursor },
    projection,
  }
}

test('labels keep lifecycle, activity, wait reason and stale evidence separate', () => {
  assert.equal(execution.runStatusLabel(run()), '编码')
  assert.equal(
    execution.runStatusLabel(run({ activity: 'waiting', wait_reason: 'approval' })),
    '等待批准',
  )
  assert.equal(execution.runStatusLabel(run({ lifecycle: 'succeeded', activity: null })), '完成')
  assert.equal(execution.runStatusLabel(run({ lifecycle: 'failed', activity: null })), '报错')
  assert.equal(execution.runStatusLabel(run({ stale: true })), '状态过期')
  assert.equal(execution.runStatusLabel(run({
    evidence: { activity: evidence('reported', { expires_at: '2026-08-17T11:59:59Z' }) },
  }), Date.parse('2026-08-17T12:00:00Z')), '状态过期')
})

test('active run selection obeys terminal binding and never compares revisions across runs', () => {
  const oldHighRevision = run({ run_id: 'old', revision: 99, last_sequence: 3 })
  const newLowRevision = run({ run_id: 'new', revision: 1, last_sequence: 8 })
  const state = snapshot({
    runs: [oldHighRevision, newLowRevision],
    terminal_bindings: [{ terminal_id: 'terminal-1', active_run_id: 'new', revision: 8 }],
  })
  assert.equal(execution.activeRunForTerminal(state, 'terminal-1').run_id, 'new')
  assert.equal(execution.activeRunForTerminal({ ...state, terminal_bindings: [] }, 'terminal-1'), null)
})

test('stream merge is cursor-monotonic and revision-monotonic per entity', () => {
  const initial = snapshot()
  const duplicateCursor = execution.applyExecutionMessage(initial, message(4, {
    runs: [run({ revision: 99, activity: 'testing' })],
  }))
  assert.strictEqual(duplicateCursor, initial)

  const next = execution.applyExecutionMessage(initial, message(5, {
    tasks: [{
      task_id: 'task-1', owner_id: 'alice', title: '状态同步', status: 'running',
      revision: 2, created_at: 1, updated_at: 2,
    }],
    assignments: [{
      assignment_id: 'assignment-1', owner_id: 'alice', task_id: 'task-1',
      status: 'accepted', revision: 3,
    }],
    runs: [run({ revision: 2, activity: 'testing' })],
    agents: [{
      agent_instance_id: 'agent-1', owner_id: 'alice', terminal_id: 'terminal-1',
      kind: 'codex', lifecycle: 'running', revision: 2,
    }],
    terminal_bindings: [{
      terminal_id: 'terminal-1', active_run_id: 'run-1',
      active_agent_instance_id: 'agent-1', revision: 2,
    }],
  }))
  assert.equal(next.as_of_sequence, 5)
  assert.equal(next.tasks[0].revision, 2)
  assert.equal(next.assignments[0].revision, 3)
  assert.equal(next.runs[0].activity, 'testing')
  assert.equal(next.agents[0].kind, 'codex')
  assert.equal(next.terminal_bindings[0].active_agent_instance_id, 'agent-1')

  const lowerRevision = execution.applyExecutionMessage(next, message(6, {
    runs: [run({ revision: 1, activity: 'planning' })],
    terminal_bindings: [{ terminal_id: 'terminal-1', active_run_id: 'wrong', revision: 1 }],
  }))
  assert.equal(lowerRevision.as_of_sequence, 6)
  assert.equal(lowerRevision.runs[0].activity, 'testing')
  assert.equal(lowerRevision.terminal_bindings[0].active_run_id, 'run-1')
})

test('same-revision projections merge only when view_sequence advances', () => {
  const initial = snapshot({
    runs: [run({ revision: 3, view_sequence: 8, activity: 'thinking' })],
  })
  const newerEvidence = execution.applyExecutionMessage(initial, message(9, {
    runs: [run({
      revision: 3,
      view_sequence: 9,
      activity: 'coding',
      evidence: {
        activity: evidence('observed', { global_sequence: 9, confidence: 0.7 }),
      },
    })],
  }))
  assert.equal(newerEvidence.runs[0].activity, 'coding')
  assert.equal(newerEvidence.runs[0].revision, 3)
  assert.equal(newerEvidence.runs[0].view_sequence, 9)

  const staleEvidence = execution.applyExecutionMessage(newerEvidence, message(10, {
    runs: [run({ revision: 3, view_sequence: 8, activity: 'planning' })],
  }))
  assert.equal(staleEvidence.as_of_sequence, 10)
  assert.equal(staleEvidence.runs[0].activity, 'coding')
})

test('projection removal and explicit resync are handled without stale entities', () => {
  const initial = snapshot()
  const removed = execution.applyExecutionMessage(initial, message(5, {
    removed: { run_ids: ['run-1'], terminal_ids: ['terminal-1'] },
  }))
  assert.deepEqual(removed.runs, [])
  assert.deepEqual(removed.terminal_bindings, [])
  assert.equal(execution.applyExecutionMessage(initial, { type: 'resync_required', after_sequence: 4 }), null)
})

test('field evidence computes freshness from explicit expiry or valid_for_ms', () => {
  const now = Date.parse('2026-08-17T12:00:00Z')
  assert.equal(execution.evidenceFreshness(evidence('reported')), 'fresh')
  assert.equal(execution.evidenceFreshness(null), 'unknown')
  assert.equal(execution.evidenceFreshness(evidence('reported', {
    observed_at: '2026-08-17T11:59:58Z', valid_for_ms: 5_000,
  }), now), 'fresh')
  assert.equal(execution.evidenceFreshness(evidence('observed', {
    observed_at: '2026-08-17T11:59:50Z', valid_for_ms: 5_000,
  }), now), 'stale')
})

test('partial snapshots normalize missing arrays for rolling-upgrade fallback', () => {
  const normalized = execution.normalizeExecutionSnapshot({
    schema: 'agentserver.execution-snapshot/1',
    as_of_sequence: 12,
    runs: [run()],
  })
  assert.equal(normalized.as_of_sequence, 12)
  assert.deepEqual(normalized.tasks, [])
  assert.deepEqual(normalized.agents, [])
  assert.deepEqual(normalized.terminal_bindings, [])
})

test('generic server projections normalize into typed frontend entities with field evidence', () => {
  const normalized = execution.normalizeExecutionSnapshot({
    schema: 'agentserver.execution-snapshot/1',
    owner_id: 'alice',
    as_of_sequence: 12,
    tasks: [{
      id: 'task-generic', kind: 'task', revision: 2, updated_at: 10,
      state: { lifecycle: 'working' },
      attributes: { title: '同步 Agent 状态', context_id: 'context-1' },
    }],
    assignments: [{
      id: 'assignment-generic', kind: 'assignment', revision: 2, updated_at: 10,
      state: { lifecycle: 'accepted' },
      attributes: { task_id: 'task-generic', terminal_id: 'terminal-1' },
    }],
    runs: [{
      id: 'run-generic', kind: 'run', revision: 3, view_sequence: 11,
      last_global_sequence: 9, updated_at: 10,
      state: {
        lifecycle: 'running', activity: 'waiting', wait_reason: 'approval', progress: 0.25,
      },
      attributes: {
        task_id: 'task-generic', assignment_id: 'assignment-generic',
        terminal_id: 'terminal-1', agent_instance_id: 'agent-generic',
        agent_kind: 'kimi', parent_run_id: 'parent-run', attempt: 2,
      },
      evidence: {
        activity: {
          source: 'reported', producer_id: 'agent:kimi', confidence: 0.8,
          recorded_at: 9, expires_at: 30, fresh: true, global_sequence: 9,
        },
      },
    }],
    agents: [{
      id: 'agent-generic', kind: 'agent_instance', revision: 1, updated_at: 8,
      state: { lifecycle: 'online', last_heartbeat_occurred_at: 7 },
      attributes: { kind: 'kimi', terminal_id: 'terminal-1', device_id: 'device-1' },
    }],
    terminal_bindings: [{
      terminal_id: 'terminal-1', active_run_id: 'run-generic',
      active_agent_instance_id: 'agent-generic', last_global_sequence: 9,
    }],
  })

  assert.equal(normalized.owner_id, 'alice')
  assert.equal(normalized.tasks[0].task_id, 'task-generic')
  assert.equal(normalized.tasks[0].title, '同步 Agent 状态')
  assert.equal(normalized.assignments[0].status, 'accepted')
  assert.equal(normalized.runs[0].run_id, 'run-generic')
  assert.equal(normalized.runs[0].activity, 'waiting')
  assert.equal(normalized.runs[0].parent_run_id, 'parent-run')
  assert.equal(normalized.runs[0].last_sequence, 9)
  assert.equal(normalized.runs[0].view_sequence, 11)
  assert.equal(normalized.runs[0].evidence.activity.observed_at, 9)
  assert.equal(normalized.runs[0].evidence.activity.stale, false)
  assert.equal(normalized.agents[0].kind, 'kimi')
  assert.equal(normalized.terminal_bindings[0].revision, 0)
  assert.equal(normalized.terminal_bindings[0].view_sequence, 12)
  assert.equal(execution.activeRunForTerminal(normalized, 'terminal-1').run_id, 'run-generic')
})

test('backend binding deltas use the event cursor so terminal completion can clear an active run', () => {
  const initial = execution.normalizeExecutionSnapshot({
    owner_id: 'alice',
    as_of_sequence: 12,
    runs: [{
      id: 'run-generic', revision: 3, last_global_sequence: 9, updated_at: 10,
      state: { lifecycle: 'running', activity: 'coding' },
      attributes: { terminal_id: 'terminal-1' },
    }],
    terminal_bindings: [{
      terminal_id: 'terminal-1', active_run_id: 'run-generic', last_global_sequence: 9,
    }],
  })
  const completed = execution.applyExecutionMessage(initial, message(13, {
    tasks: [], assignments: [], agents: [], terminals: [],
    runs: [{
      id: 'run-generic', revision: 4, last_global_sequence: 13, updated_at: 13,
      state: { lifecycle: 'succeeded', summary: 'Done' },
      attributes: { terminal_id: 'terminal-1' },
    }],
    terminal_bindings: [{
      terminal_id: 'terminal-1', active_run_id: null,
      active_agent_instance_id: null, last_global_sequence: 0,
    }],
  }))

  assert.equal(completed.runs[0].lifecycle, 'succeeded')
  assert.equal(completed.terminal_bindings[0].revision, 0)
  assert.equal(completed.terminal_bindings[0].view_sequence, 13)
  assert.equal(completed.terminal_bindings[0].active_run_id, null)
  assert.equal(execution.activeRunForTerminal(completed, 'terminal-1'), null)
})

test('single-flight coalesces concurrent resync and permits the next generation', async () => {
  const singleFlight = execution.createSingleFlight()
  let calls = 0
  let release
  const first = singleFlight(async () => {
    calls += 1
    await new Promise((resolve) => { release = resolve })
    return 'snapshot-1'
  })
  const duplicate = singleFlight(async () => {
    calls += 1
    return 'wrong'
  })
  assert.strictEqual(first, duplicate)
  await Promise.resolve()
  assert.equal(calls, 1)
  release()
  assert.equal(await first, 'snapshot-1')
  await Promise.resolve()
  assert.equal(await singleFlight(async () => {
    calls += 1
    return 'snapshot-2'
  }), 'snapshot-2')
  assert.equal(calls, 2)
})

test('parent grouping uses event sequence, not per-run revision', () => {
  const root = run({ run_id: 'root', parent_run_id: null, last_sequence: 1, revision: 30 })
  const later = run({ run_id: 'later', parent_run_id: 'root', last_sequence: 4, revision: 1 })
  const earlier = run({ run_id: 'earlier', parent_run_id: 'root', last_sequence: 2, revision: 20 })
  const state = snapshot({ runs: [later, root, earlier] })
  assert.deepEqual(execution.runsByParent(state, 'root').map((item) => item.run_id), ['earlier', 'later'])
})

test('relation edges are authoritative for parent grouping and support multiple DAG roots', () => {
  const firstRoot = run({ run_id: 'root-a', parent_run_id: null, last_sequence: 1 })
  const secondRoot = run({ run_id: 'root-b', parent_run_id: null, last_sequence: 2 })
  const child = run({
    run_id: 'child',
    // This legacy attribute is deliberately wrong; relation edges win.
    parent_run_id: 'legacy-root',
    last_sequence: 3,
  })
  const state = execution.normalizeExecutionSnapshot({
    schema: 'agentserver.execution-snapshot/1',
    as_of_sequence: 9,
    runs: [firstRoot, secondRoot, child],
    relations: [
      {
        id: 'relation-a', relation: 'parent_run',
        source: { kind: 'run', id: 'root-a' },
        target: { kind: 'run', id: 'child' },
      },
      {
        id: 'relation-b', relation: 'parent_run',
        source: { kind: 'run', id: 'root-b' },
        target: { kind: 'run', id: 'child' },
      },
    ],
  })
  assert.deepEqual(execution.parentRunIds(state, 'child'), ['root-a', 'root-b'])
  assert.deepEqual(execution.runsByParent(state, 'root-a').map((item) => item.run_id), ['child'])
  assert.deepEqual(
    execution.runTreeRoots(state, state.runs.find((item) => item.run_id === 'child'))
      .map((item) => item.run_id),
    ['root-a', 'root-b'],
  )

  const replaced = execution.applyExecutionMessage(state, message(10, {
    relations: [],
  }))
  assert.deepEqual(replaced.relations, [])
  assert.deepEqual(execution.parentRunIds(replaced, 'child'), [])
})

test('next evidence expiry selects the nearest future field boundary', () => {
  const now = Date.parse('2026-08-17T12:00:00Z')
  const state = snapshot({
    runs: [run({
      evidence: {
        activity: evidence('reported', { expires_at: now + 5_000 }),
        progress: evidence('reported', { expires_at: now + 2_000 }),
        summary: evidence('stale', { expires_at: now + 1_000 }),
      },
    })],
  })
  assert.equal(execution.nextEvidenceExpiry(state, now), now + 2_000)
  assert.equal(execution.nextEvidenceExpiry(state, now + 5_000), null)
})
