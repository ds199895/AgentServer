import { chromium } from 'playwright'

const base = process.env.E2E_BASE || 'http://127.0.0.1:18124'
const password = process.env.E2E_PASSWORD || 'e2e-test-password'

function fail(message) {
  throw new Error(message)
}

async function waitForPaneCount(page, expected) {
  await page.waitForFunction((count) => (
    document.querySelectorAll('[data-terminal-pane-root="true"]').length === count
  ), expected)
}

async function waitForPaneTabCount(page, paneNumber, expected) {
  await page.waitForFunction(({ number, count }) => {
    const pane = document.querySelector(`[data-terminal-pane-number="${number}"]`)
    return pane?.querySelectorAll('[role="tab"]').length === count
  }, { number: paneNumber, count: expected })
}

async function waitForTabMode(page, paneNumber, sessionId, mode) {
  await page.waitForFunction(({ number, id, expectedMode }) => {
    const pane = document.querySelector(`[data-terminal-pane-number="${number}"]`)
    return pane?.querySelector(`[role="tab"][data-terminal-tab-id="${id}"]`)
      ?.getAttribute('data-terminal-tab-mode') === expectedMode
  }, { number: paneNumber, id: sessionId, expectedMode: mode })
}

async function paneSnapshot(page) {
  return page.locator('[data-terminal-pane-root="true"]').evaluateAll((nodes) => (
    nodes.map((node) => {
      const tabs = [...node.querySelectorAll('[role="tab"]')]
      return {
        number: Number(node.getAttribute('data-terminal-pane-number')),
        leafId: node.getAttribute('data-terminal-leaf-id'),
        empty: node.getAttribute('data-terminal-pane-empty') === 'true',
        tabs: tabs.map((tab) => tab.getAttribute('data-terminal-tab-id')),
        modes: Object.fromEntries(tabs.map((tab) => [
          tab.getAttribute('data-terminal-tab-id'),
          tab.getAttribute('data-terminal-tab-mode'),
        ])),
        active: tabs.find((tab) => tab.getAttribute('aria-selected') === 'true')
          ?.getAttribute('data-terminal-tab-id') || null,
      }
    }).sort((first, second) => first.number - second.number)
  ))
}

function paneFromSnapshot(snapshot, number) {
  const pane = snapshot.find((item) => item.number === number)
  if (!pane) fail(`pane P${number} is missing: ${JSON.stringify(snapshot)}`)
  return pane
}

async function assertPaneTablists(page, expectedPaneCount) {
  const panes = page.locator('[data-terminal-pane-root="true"]')
  const paneCount = await panes.count()
  if (paneCount !== expectedPaneCount) {
    fail(`expected ${expectedPaneCount} pane frames, received ${paneCount}`)
  }
  const totalTablists = await page.getByRole('tablist').count()
  if (totalTablists !== expectedPaneCount) {
    fail(`expected one tablist per pane (${expectedPaneCount}), received ${totalTablists}`)
  }
  for (let index = 0; index < paneCount; index += 1) {
    const nestedTablists = await panes.nth(index).getByRole('tablist').count()
    if (nestedTablists !== 1) {
      fail(`pane frame ${index + 1} contains ${nestedTablists} tablists instead of one`)
    }
  }
}

async function activatePaneTab(page, paneNumber, sessionId) {
  const tab = page.locator(
    `[data-terminal-pane-number="${paneNumber}"] [role="tab"][data-terminal-tab-id="${sessionId}"]`,
  )
  await tab.click()
  await page.waitForFunction(({ number, id }) => {
    const pane = document.querySelector(`[data-terminal-pane-number="${number}"]`)
    return pane?.querySelector(`[role="tab"][data-terminal-tab-id="${id}"]`)
      ?.getAttribute('aria-selected') === 'true'
  }, { number: paneNumber, id: sessionId })
}

async function detachPaneTerminal(page, paneNumber, sessionId) {
  const pane = page.locator(`[data-terminal-pane-number="${paneNumber}"]`)
  const tab = pane.locator(`[role="tab"][data-terminal-tab-id="${sessionId}"]`)
  await tab.locator('..').getByRole('button', {
    name: new RegExp(`^从窗格 ${paneNumber} 移除终端 .*，后台继续运行$`),
  }).click()
  await page.waitForFunction((id) => (
    !document.querySelector(`[role="tab"][data-terminal-tab-id="${id}"]`)
  ), sessionId)
}

function groupSessionPreviewButton(page, sessionId) {
  return page.getByRole('list', { name: '本机 终端', exact: true }).getByRole('button', {
    name: new RegExp(`${sessionId.slice(0, 8)}，双击固定$`),
  })
}

function groupSessionItem(page, sessionId) {
  return groupSessionPreviewButton(page, sessionId).locator('..')
}

async function ensureLocalGroupExpanded(page) {
  const expanded = page.getByRole('button', { name: /^收起 本机 终端组，/ })
  if (await expanded.count()) return
  await page.getByRole('button', { name: /^展开 本机 终端组，/ }).click()
  await page.getByRole('list', { name: '本机 终端', exact: true }).waitFor()
}

async function focusEmptyPane(page, paneNumber) {
  await page.getByRole('region', { name: `空终端窗格 ${paneNumber}`, exact: true }).click()
  await page.waitForURL(/\/terminals\/?$/)
}

async function dragPaneTab(page, {
  sessionId,
  sourcePane,
  targetPane,
  beforeSessionId = null,
}) {
  const source = page.locator(
    `[data-terminal-pane-number="${sourcePane}"] [data-terminal-drag-id="${sessionId}"]`,
  )
  await source.scrollIntoViewIfNeeded()
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer())
  try {
    await source.dispatchEvent('dragstart', { dataTransfer })
    await page.waitForFunction(() => (
      document.querySelector('[data-mounted-terminal-count]')
        ?.getAttribute('data-terminal-tab-dragging') === 'true'
    ))

    const target = beforeSessionId
      ? page.locator(
          `[data-terminal-pane-number="${targetPane}"] [data-terminal-drag-wrapper="${beforeSessionId}"]`,
        )
      : page.locator(`[data-terminal-pane-drop-zone="${targetPane}"]`)
    await target.waitFor()
    const box = await target.boundingBox()
    if (!box) fail(`drag target P${targetPane} has no bounding box`)
    const clientX = beforeSessionId ? box.x + 2 : box.x + box.width / 2
    const clientY = box.y + box.height / 2
    await target.dispatchEvent('dragover', { dataTransfer, clientX, clientY })
    await page.waitForFunction(({ pane, before }) => {
      const targetPaneNode = document.querySelector(`[data-terminal-pane-number="${pane}"]`)
      if (!targetPaneNode) return false
      if (before) {
        return targetPaneNode
          .querySelector(`[data-terminal-drag-wrapper="${before}"]`)
          ?.getAttribute('data-terminal-drop-active') === 'true'
      }
      return targetPaneNode
        .querySelector(`[data-terminal-pane-drop-zone="${pane}"]`)
        ?.getAttribute('data-terminal-drop-active') === 'true'
    }, { pane: targetPane, before: beforeSessionId })
    await target.dispatchEvent('drop', { dataTransfer, clientX, clientY })
    await page.waitForFunction(() => (
      document.querySelector('[data-mounted-terminal-count]')
        ?.getAttribute('data-terminal-tab-dragging') === 'false'
    ))
  } finally {
    await dataTransfer.dispose()
    const stillDragging = await page.locator('[data-terminal-tab-dragging="true"]').count()
    if (stillDragging) await page.keyboard.press('Escape')
  }
}

async function dragPaneTabWithMouse(page, {
  sessionId,
  sourcePane,
  targetPane,
}) {
  const source = page.locator(
    `[data-terminal-pane-number="${sourcePane}"] [data-terminal-drag-id="${sessionId}"]`,
  )
  await source.scrollIntoViewIfNeeded()
  const sourceBox = await source.boundingBox()
  if (!sourceBox) fail(`native drag source ${sessionId} in P${sourcePane} has no bounding box`)
  let mouseDown = false
  try {
    const sourceX = sourceBox.x + sourceBox.width / 2
    const sourceY = sourceBox.y + sourceBox.height / 2
    await page.mouse.move(sourceX, sourceY)
    await page.mouse.down()
    mouseDown = true
    await page.mouse.move(sourceX + 14, sourceY + 2, { steps: 4 })
    await page.waitForFunction(() => (
      document.querySelector('[data-mounted-terminal-count]')
        ?.getAttribute('data-terminal-tab-dragging') === 'true'
    ))

    const target = page.locator(`[data-terminal-pane-drop-zone="${targetPane}"]`)
    await target.waitFor()
    const targetBox = await target.boundingBox()
    if (!targetBox) fail(`native drag target P${targetPane} has no bounding box`)
    await page.mouse.move(
      targetBox.x + targetBox.width / 2,
      targetBox.y + targetBox.height / 2,
      { steps: 14 },
    )
    await page.waitForFunction((pane) => (
      document.querySelector(`[data-terminal-pane-drop-zone="${pane}"]`)
        ?.getAttribute('data-terminal-drop-active') === 'true'
    ), targetPane)
    await page.mouse.up()
    mouseDown = false
    await page.waitForFunction(() => (
      document.querySelector('[data-mounted-terminal-count]')
        ?.getAttribute('data-terminal-tab-dragging') === 'false'
    ))
  } finally {
    if (mouseDown) await page.mouse.up()
    const stillDragging = await page.locator('[data-terminal-tab-dragging="true"]').count()
    if (stillDragging) await page.keyboard.press('Escape')
  }
}

async function backendHasSessions(page, sessionIds) {
  return page.evaluate(async (ids) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return false
    const sessions = await response.json()
    const available = new Set(sessions.map((session) => session.id))
    return ids.every((id) => available.has(id))
  }, sessionIds)
}

async function backendLacksSessions(page, sessionIds) {
  return page.evaluate(async (ids) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return false
    const sessions = await response.json()
    const available = new Set(sessions.map((session) => session.id))
    return ids.every((id) => !available.has(id))
  }, sessionIds)
}

async function visibleTerminalIds(page) {
  return page.locator('[data-terminal-visible="true"]').evaluateAll((nodes) => (
    nodes.map((node) => node.getAttribute('data-terminal-id')).filter(Boolean).sort()
  ))
}

async function createLocalTerminal(page, name, requireWorkspace = false) {
  return page.evaluate(async ({ terminalName, workspaceRequired }) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: terminalName }),
    })
    if (!response.ok) throw new Error(`failed to create terminal: ${response.status}`)
    const session = await response.json()
    if (!session.id) throw new Error('created terminal has no id')
    if (workspaceRequired && session.workspace?.available !== true) {
      throw new Error(`created terminal has no workspace: ${session.workspace?.error || 'unknown error'}`)
    }
    return session.id
  }, { terminalName: name, workspaceRequired: requireWorkspace })
}

async function assertWorkspaceArtifactAndImageContracts(page, sessionId) {
  await page.evaluate(async (id) => {
    const request = async (path, init) => {
      const response = await fetch(path, init)
      if (!response.ok) {
        const detail = await response.text()
        throw new Error(`${init?.method || 'GET'} ${path} failed: ${response.status} ${detail}`)
      }
      return response
    }

    const workspaceResponse = await request(
      `/api/terminals/${encodeURIComponent(id)}/workspace?path=`,
    )
    const workspace = await workspaceResponse.json()
    if (!Array.isArray(workspace.entries)) throw new Error('workspace entries are not an array')
    const readme = workspace.entries.find((entry) => entry.path === 'README.md')
    if (!readme || readme.kind !== 'file') throw new Error('README.md missing from terminal workspace')

    const resolveResponse = await request(
      `/api/terminals/${encodeURIComponent(id)}/files/resolve`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'README.md' }),
      },
    )
    const grant = await resolveResponse.json()
    if (!grant.id || grant.terminal_id !== id || grant.path !== 'README.md' || !grant.etag) {
      throw new Error('invalid file grant payload')
    }

    const contentResponse = await request(
      `/api/files/${encodeURIComponent(grant.id)}/content?terminal_id=${encodeURIComponent(id)}`,
      { headers: { Range: 'bytes=0-15' } },
    )
    if (contentResponse.status !== 206) {
      throw new Error(`range read returned ${contentResponse.status}, expected 206`)
    }
    if (!contentResponse.headers.get('content-range')?.startsWith('bytes 0-15/')) {
      throw new Error('range read has no valid Content-Range header')
    }
    if (contentResponse.headers.get('x-content-type-options') !== 'nosniff') {
      throw new Error('range read has no nosniff protection')
    }
    if (!(await contentResponse.text()).startsWith('# AgentServer')) {
      throw new Error('range read returned unexpected bytes')
    }

    const createArtifactResponse = await request(
      `/api/terminals/${encodeURIComponent(id)}/artifacts`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'contract-check',
          path: 'README.md',
          name: 'README.md',
          media_type: grant.media_type,
          size: grant.size,
          version: grant.version,
          source: 'e2e-contract',
        }),
      },
    )
    if (createArtifactResponse.status !== 201) {
      throw new Error(`artifact creation returned ${createArtifactResponse.status}, expected 201`)
    }
    const createdArtifact = await createArtifactResponse.json()
    if (!createdArtifact.id || createdArtifact.path !== 'README.md') {
      throw new Error('artifact creation returned an invalid event')
    }
    const artifacts = await (await request(
      `/api/terminals/${encodeURIComponent(id)}/artifacts`,
    )).json()
    const persisted = Array.isArray(artifacts)
      ? artifacts.find((event) => event.id === createdArtifact.id)
      : null
    if (!persisted || persisted.source !== 'e2e-contract' || persisted.schema_version !== 1) {
      throw new Error('created artifact was not present in the durable snapshot')
    }

    const imageResult = await (await request(
      `/api/terminals/${encodeURIComponent(id)}/read-image`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'web_dist/favicon.png' }),
      },
    )).json()
    if (imageResult.model_content_format !== 'openai-responses') {
      throw new Error('read-image did not identify its model content adapter format')
    }
    const imageBlock = imageResult.model_content?.find((block) => block.type === 'input_image')
    if (!imageBlock?.image_url?.startsWith('data:image/png;base64,')) {
      throw new Error('read-image did not return an inline model image block')
    }
    if (!imageResult.attachment?.id?.startsWith('sha256:')) {
      throw new Error('read-image did not return a content-addressed attachment')
    }
    const attachmentResponse = await request(imageResult.attachment.url)
    if (attachmentResponse.headers.get('content-type') !== 'image/png') {
      throw new Error('immutable attachment has an unexpected media type')
    }
    if ((await attachmentResponse.arrayBuffer()).byteLength !== imageResult.attachment.size) {
      throw new Error('immutable attachment size does not match its reference')
    }
  }, sessionId)
}

const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
})
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const pageErrors = []
const consoleErrors = []
const terminalCreateRequests = []
const terminalDeleteRequests = []
const contractName = `contract-check-${Date.now().toString(36)}`
const switchName = `${contractName}-switch`
const activePaneName = `${contractName}-active-pane`
const scalePrefix = `${contractName}-scale-`
let terminalId = ''
let switchTerminalId = ''
let activePaneTerminalId = ''

page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})
page.on('request', (request) => {
  const pathname = new URL(request.url()).pathname
  if (
    request.method() === 'POST' &&
    (pathname === '/api/terminals' || /^\/api\/devices\/[^/]+\/terminals$/.test(pathname))
  ) {
    terminalCreateRequests.push(request.url())
  }
  if (request.method() === 'DELETE' && /^\/api\/terminals\/[^/]+$/.test(pathname)) {
    terminalDeleteRequests.push(request.url())
  }
})

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '进入控制台' }).waitFor()
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.getByRole('navigation', { name: '主导航' }).waitFor()

  terminalId = await createLocalTerminal(page, contractName, true)
  switchTerminalId = await createLocalTerminal(page, switchName)
  activePaneTerminalId = await createLocalTerminal(page, activePaneName)
  await assertWorkspaceArtifactAndImageContracts(page, terminalId)

  // Compatibility guard: older agents can omit services. API normalization
  // must still let the UI construct device groups, panes, and tab strips.
  await page.route('**/api/terminals', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }
    // Keep the compatibility response independent from the page request. The
    // page can navigate while this handler is running, which aborts Playwright
    // forwarding on newer Chromium builds even after the server returned
    // successfully. Node's fetch is not tied to that navigation lifecycle.
    const cookie = await route.request().headerValue('cookie')
    const response = await fetch(route.request().url(), {
      headers: cookie ? { cookie } : {},
    })
    const sessions = await response.json()
    for (const session of sessions) delete session.services
    await route.fulfill({ status: response.status, json: sessions })
  })

  // Session state also arrives over /ws/sessions, so the same compatibility
  // guard has to cover pushed snapshots — otherwise the first push would hand
  // the UI a `services` array the HTTP guard above just removed, and the
  // older-agent path would silently stop being exercised.
  await page.routeWebSocket('**/ws/sessions', (ws) => {
    const server = ws.connectToServer()
    server.onMessage((message) => {
      try {
        const sessions = JSON.parse(String(message))
        for (const session of sessions) delete session.services
        ws.send(JSON.stringify(sessions))
      } catch {
        ws.send(message)
      }
    })
    ws.onMessage((message) => server.send(message))
  })

  // Backend sessions must not populate a fresh workspace. Explicitly clear
  // storage and enter /terminals: P1 exists as an empty editor group.
  await page.evaluate(() => window.localStorage.clear())
  await page.goto(`${base}/terminals`, { waitUntil: 'networkidle' })
  await waitForPaneCount(page, 1)
  await assertPaneTablists(page, 1)
  let snapshot = await paneSnapshot(page)
  const initialP1 = paneFromSnapshot(snapshot, 1)
  if (!initialP1.empty || initialP1.tabs.length !== 0 || initialP1.active !== null) {
    fail(`fresh terminal workspace was not empty: ${JSON.stringify(snapshot)}`)
  }
  if (!/\/terminals\/?$/.test(new URL(page.url()).pathname)) {
    fail(`empty workspace did not stay on /terminals: ${page.url()}`)
  }

  const deviceGroups = page.getByRole('list', { name: '终端设备组' })
  await deviceGroups.waitFor()
  if (await deviceGroups.getByRole('tab').count() !== 0) {
    fail('global device navigation exposes pane tabs')
  }

  // Splitting is a pure layout operation: both descendants are empty and no
  // terminal POST is allowed. The second split establishes nested P1/P2/P3.
  const createCountBeforeSplit = terminalCreateRequests.length
  await page.getByRole('button', {
    name: '将窗格 1 向右分屏，新窗格为空',
    exact: true,
  }).click()
  await waitForPaneCount(page, 2)
  await page.getByRole('button', {
    name: '将窗格 2 向下分屏，新窗格为空',
    exact: true,
  }).click()
  await waitForPaneCount(page, 3)
  await assertPaneTablists(page, 3)
  snapshot = await paneSnapshot(page)
  if (snapshot.some((pane) => !pane.empty || pane.tabs.length || pane.active !== null)) {
    fail(`pure split created a terminal or a non-empty pane: ${JSON.stringify(snapshot)}`)
  }
  if (terminalCreateRequests.length !== createCountBeforeSplit) {
    fail('pure split issued a terminal POST request')
  }
  const paneLeafIds = snapshot.map((pane) => pane.leafId)

  // Both nested split sashes remain keyboard operable.
  const horizontalSash = page.getByRole('separator', { name: '调整左右终端宽度' })
  const verticalSash = page.getByRole('separator', { name: '调整上下终端高度' })
  await horizontalSash.focus()
  await page.keyboard.press('End')
  await page.waitForFunction(() => (
    document.querySelector('[role="separator"][aria-label="调整左右终端宽度"]')
      ?.getAttribute('aria-valuenow') === '85'
  ))
  await verticalSash.focus()
  await page.keyboard.press('Home')
  await page.waitForFunction(() => (
    document.querySelector('[role="separator"][aria-label="调整上下终端高度"]')
      ?.getAttribute('aria-valuenow') === '15'
  ))

  // P3 is focused by the second split. A single device-group click previews A;
  // clicking B replaces A without touching either backend process.
  await ensureLocalGroupExpanded(page)
  const deleteCountBeforePreview = terminalDeleteRequests.length
  await groupSessionPreviewButton(page, terminalId).click()
  await waitForPaneTabCount(page, 3, 1)
  await waitForTabMode(page, 3, terminalId, 'preview')
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !== JSON.stringify([terminalId]) ||
    paneFromSnapshot(snapshot, 3).active !== terminalId
  ) {
    fail(`A did not open as P3 preview: ${JSON.stringify(snapshot)}`)
  }

  await groupSessionPreviewButton(page, switchTerminalId).click()
  await waitForPaneTabCount(page, 3, 1)
  await waitForTabMode(page, 3, switchTerminalId, 'preview')
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !== JSON.stringify([switchTerminalId]) ||
    snapshot.some((pane) => pane.tabs.includes(terminalId))
  ) {
    fail(`B did not replace A's preview assignment: ${JSON.stringify(snapshot)}`)
  }
  if (terminalDeleteRequests.length !== deleteCountBeforePreview) {
    fail('preview replacement issued a terminal DELETE request')
  }
  if (!(await backendHasSessions(page, [terminalId, switchTerminalId]))) {
    fail('preview replacement terminated A or B in the backend')
  }

  // A real device-group double click makes B durable. A subsequent click can
  // coexist as the pane's one transient preview; the explicit Pin path is
  // exercised below when C is opened on mobile-capable controls.
  await groupSessionPreviewButton(page, switchTerminalId).dblclick()
  await waitForTabMode(page, 3, switchTerminalId, 'pinned')
  await groupSessionPreviewButton(page, terminalId).click()
  await waitForPaneTabCount(page, 3, 2)
  await waitForTabMode(page, 3, terminalId, 'preview')
  snapshot = await paneSnapshot(page)
  const p3WithPinnedAndPreview = paneFromSnapshot(snapshot, 3)
  if (
    JSON.stringify(p3WithPinnedAndPreview.tabs) !== JSON.stringify([switchTerminalId, terminalId]) ||
    p3WithPinnedAndPreview.modes[switchTerminalId] !== 'pinned' ||
    p3WithPinnedAndPreview.modes[terminalId] !== 'preview'
  ) {
    fail(`pinned B did not coexist with preview A: ${JSON.stringify(snapshot)}`)
  }

  // Pane tabs are explicit view assignments. Dragging a pinned tab to an
  // empty pane and back preserves its mode/order, focuses the drop pane, and
  // cannot create or terminate a backend process.
  const createCountBeforeDrag = terminalCreateRequests.length
  const deleteCountBeforeDrag = terminalDeleteRequests.length
  await dragPaneTabWithMouse(page, {
    sessionId: switchTerminalId,
    sourcePane: 3,
    targetPane: 2,
  })
  await waitForPaneTabCount(page, 2, 1)
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 2).tabs) !== JSON.stringify([switchTerminalId]) ||
    paneFromSnapshot(snapshot, 2).modes[switchTerminalId] !== 'pinned' ||
    paneFromSnapshot(snapshot, 2).active !== switchTerminalId ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !== JSON.stringify([terminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[terminalId] !== 'preview'
  ) {
    fail(`dragging pinned B from P3 to P2 corrupted pane state: ${JSON.stringify(snapshot)}`)
  }
  if (!new URL(page.url()).pathname.endsWith(`/terminal/${switchTerminalId}`)) {
    fail(`dragging B did not synchronize the active route: ${page.url()}`)
  }
  if (
    await page.locator('[data-terminal-pane-number="2"]')
      .getAttribute('data-terminal-pane-focused') !== 'true'
  ) {
    fail('dragging B did not make P2 the focused pane')
  }
  await dragPaneTab(page, {
    sessionId: switchTerminalId,
    sourcePane: 2,
    targetPane: 3,
    beforeSessionId: terminalId,
  })
  snapshot = await paneSnapshot(page)
  if (
    !paneFromSnapshot(snapshot, 2).empty ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !==
      JSON.stringify([switchTerminalId, terminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[switchTerminalId] !== 'pinned' ||
    paneFromSnapshot(snapshot, 3).modes[terminalId] !== 'preview'
  ) {
    fail(`dragging pinned B back to P3 lost its insertion order: ${JSON.stringify(snapshot)}`)
  }

  await dragPaneTab(page, {
    sessionId: switchTerminalId,
    sourcePane: 3,
    targetPane: 3,
  })
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !==
      JSON.stringify([terminalId, switchTerminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[terminalId] !== 'preview'
  ) {
    fail(`same-pane drag did not move B to the end: ${JSON.stringify(snapshot)}`)
  }
  await dragPaneTab(page, {
    sessionId: switchTerminalId,
    sourcePane: 3,
    targetPane: 3,
    beforeSessionId: terminalId,
  })
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !==
      JSON.stringify([switchTerminalId, terminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[terminalId] !== 'preview'
  ) {
    fail(`same-pane drag did not restore B before A: ${JSON.stringify(snapshot)}`)
  }

  // Moving a preview preserves preview mode. If the target already has a
  // preview, that old preview is merely detached (its backend stays alive).
  await focusEmptyPane(page, 1)
  await groupSessionPreviewButton(page, activePaneTerminalId).click()
  await waitForTabMode(page, 1, activePaneTerminalId, 'preview')
  await dragPaneTab(page, {
    sessionId: terminalId,
    sourcePane: 3,
    targetPane: 1,
  })
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 1).tabs) !== JSON.stringify([terminalId]) ||
    paneFromSnapshot(snapshot, 1).modes[terminalId] !== 'preview' ||
    paneFromSnapshot(snapshot, 1).active !== terminalId ||
    snapshot.some((pane) => pane.tabs.includes(activePaneTerminalId)) ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !== JSON.stringify([switchTerminalId])
  ) {
    fail(`preview drag did not replace only P1's old preview: ${JSON.stringify(snapshot)}`)
  }
  await dragPaneTab(page, {
    sessionId: terminalId,
    sourcePane: 1,
    targetPane: 3,
  })
  snapshot = await paneSnapshot(page)
  if (
    !paneFromSnapshot(snapshot, 1).empty ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !==
      JSON.stringify([switchTerminalId, terminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[terminalId] !== 'preview'
  ) {
    fail(`dragging preview A back to P3 did not restore the workspace: ${JSON.stringify(snapshot)}`)
  }
  if (
    terminalCreateRequests.length !== createCountBeforeDrag ||
    terminalDeleteRequests.length !== deleteCountBeforeDrag
  ) {
    fail('pane tab dragging issued a terminal create/delete request')
  }
  if (!(await backendHasSessions(page, [terminalId, switchTerminalId, activePaneTerminalId]))) {
    fail('pane tab dragging changed backend terminal lifetime')
  }

  // Selecting an unassigned terminal after changing the active pane places it
  // into that pane only; other pane membership and active tabs stay independent.
  await focusEmptyPane(page, 1)
  await groupSessionPreviewButton(page, activePaneTerminalId).click()
  await waitForPaneTabCount(page, 1, 1)
  await waitForTabMode(page, 1, activePaneTerminalId, 'preview')
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 1).tabs) !== JSON.stringify([activePaneTerminalId]) ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !==
      JSON.stringify([switchTerminalId, terminalId])
  ) {
    fail(`active-pane preview landed in the wrong group: ${JSON.stringify(snapshot)}`)
  }
  await activatePaneTab(page, 3, switchTerminalId)
  if (paneFromSnapshot(await paneSnapshot(page), 1).active !== activePaneTerminalId) {
    fail('switching P3 changed P1 active tab')
  }
  await activatePaneTab(page, 3, terminalId)

  // Preview tabs are runtime-only. Verify storage contains B but neither A nor
  // the P1 preview, navigate away to avoid a deep-link reopening a preview,
  // reload, and return to the terminal workspace.
  await page.waitForFunction(({ pinned, transient }) => {
    const raw = window.localStorage.getItem('agentserver:terminal-layout-v1')
    if (!raw) return false
    const payload = JSON.parse(raw)
    const ids = []
    const collect = (node) => {
      if (node.type === 'leaf') ids.push(...node.tabs)
      else node.children.forEach(collect)
    }
    collect(payload.layout)
    return ids.includes(pinned) && transient.every((id) => !ids.includes(id))
  }, { pinned: switchTerminalId, transient: [terminalId, activePaneTerminalId] })
  await page.getByRole('navigation', { name: '主导航' })
    .getByRole('button', { name: /^设备列表/ }).click()
  await page.waitForURL((url) => url.pathname === '/')
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('navigation', { name: '主导航' })
    .getByRole('button', { name: /^终端/ }).click()
  await waitForPaneCount(page, 3)
  await assertPaneTablists(page, 3)
  snapshot = await paneSnapshot(page)
  if (
    JSON.stringify(snapshot.map((pane) => pane.leafId)) !== JSON.stringify(paneLeafIds) ||
    !paneFromSnapshot(snapshot, 1).empty ||
    !paneFromSnapshot(snapshot, 2).empty ||
    JSON.stringify(paneFromSnapshot(snapshot, 3).tabs) !== JSON.stringify([switchTerminalId]) ||
    paneFromSnapshot(snapshot, 3).modes[switchTerminalId] !== 'pinned' ||
    snapshot.some((pane) => pane.tabs.includes(terminalId) || pane.tabs.includes(activePaneTerminalId))
  ) {
    fail(`reload did not keep only pinned tabs: ${JSON.stringify(snapshot)}`)
  }
  if (
    await page.getByRole('separator', { name: '调整左右终端宽度' }).getAttribute('aria-valuenow') !== '85' ||
    await page.getByRole('separator', { name: '调整上下终端高度' }).getAttribute('aria-valuenow') !== '15'
  ) {
    fail('reload lost a persisted sash ratio')
  }

  // Reopen A as a preview to exercise the real workspace UI and the durable
  // Artifact/read-image event created by the backend contract above.
  await ensureLocalGroupExpanded(page)
  await groupSessionPreviewButton(page, terminalId).click()
  await waitForPaneTabCount(page, 3, 2)
  const primaryPane = page.locator(`[data-terminal-id="${terminalId}"]`)

  // Real keyboard input (including erase) must reach the PTY exactly, and a
  // burst of terminal switches must restore a non-zero renderer without a
  // wheel event or other manual repaint trigger.
  await primaryPane.locator('[data-terminal-recovering="false"]').waitFor()
  await primaryPane.locator('.terminal-host').click()
  await page.keyboard.type("printf 'input-delete-wrong'")
  // Remove the closing quote plus all five letters in "wrong".
  for (let index = 0; index < 6; index += 1) await page.keyboard.press('Backspace')
  await page.keyboard.type("right'")
  await page.keyboard.press('Enter')
  // Switching tabs must not rebuild any renderer. addon-webgl shares one
  // TextureAtlas across identically configured panes, so a rebuild here both
  // burned through Chromium's active-context budget and wiped the glyph cache
  // out from under every other mounted terminal, which then drew garbage.
  await page.evaluate(() => {
    window.__webglContexts = 0
    const getContext = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
      if (typeof type === 'string' && type.startsWith('webgl')) window.__webglContexts += 1
      return getContext.call(this, type, ...rest)
    }
  })
  for (let index = 0; index < 8; index += 1) {
    await activatePaneTab(page, 3, switchTerminalId)
    await activatePaneTab(page, 3, terminalId)
  }
  const rebuiltContexts = await page.evaluate(() => window.__webglContexts)
  if (rebuiltContexts !== 0) {
    fail(`16 terminal switches rebuilt ${rebuiltContexts} WebGL context(s)`)
  }
  const rendererMetrics = await primaryPane.evaluate((node) => {
    const host = node.querySelector('.terminal-host')
    const screen = node.querySelector('.xterm-screen')
    const hostRect = host?.getBoundingClientRect()
    const screenRect = screen?.getBoundingClientRect()
    return {
      visible: node.getAttribute('data-terminal-visible') === 'true',
      hostWidth: hostRect?.width || 0,
      hostHeight: hostRect?.height || 0,
      screenWidth: screenRect?.width || 0,
      screenHeight: screenRect?.height || 0,
    }
  })
  if (
    !rendererMetrics.visible ||
    rendererMetrics.hostWidth <= 0 ||
    rendererMetrics.hostHeight <= 0 ||
    rendererMetrics.screenWidth <= 0 ||
    rendererMetrics.screenHeight <= 0
  ) {
    fail(`terminal renderer did not recover after switching: ${JSON.stringify(rendererMetrics)}`)
  }
  const inputSnapshot = await page.evaluate((id) => new Promise((resolve) => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/ws/terminal/${id}`)
    socket.binaryType = 'arraybuffer'
    const decoder = new TextDecoder()
    let output = ''
    const timeout = setTimeout(() => {
      socket.close()
      resolve({ ok: false, output })
    }, 3000)
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        output += decoder.decode(new Uint8Array(event.data), { stream: true })
      }
      if (event.data === '\x01[snapshot-complete]') {
        clearTimeout(timeout)
        socket.close()
        resolve({ ok: output.includes('input-delete-right'), output })
      }
    }
    socket.onerror = () => {
      clearTimeout(timeout)
      resolve({ ok: false, output })
    }
  }), terminalId)
  if (!inputSnapshot.ok) {
    fail(`keyboard input or Backspace did not reach the PTY exactly: ${JSON.stringify(inputSnapshot.output.slice(-500))}`)
  }

  await primaryPane.getByRole('button', { name: '打开工作区文件' }).click()
  const workspacePane = page.getByRole('complementary', { name: /工作区/ })
  await workspacePane.waitFor()
  await workspacePane.getByRole('tree').waitFor()
  await workspacePane.getByRole('treeitem', { name: /README\.md/ }).click()
  await workspacePane.locator('.cm-content').filter({ hasText: '# AgentServer' }).waitFor()
  await workspacePane.getByRole('button', { name: '关闭文件预览' }).click()
  await workspacePane.getByRole('treeitem', { name: /web_dist/ }).click()
  await workspacePane.getByRole('treeitem', { name: /favicon\.png/ }).click()
  await workspacePane.getByRole('img', { name: 'favicon.png' }).waitFor()
  await workspacePane.getByRole('button', { name: /Artifacts/ }).click()
  await workspacePane.getByText('不可变').first().waitFor()
  await workspacePane.getByRole('button', { name: '关闭工作区' }).click()

  // The pane X only detaches A. It has no confirmation and cannot emit DELETE.
  const deleteCountBeforeDetach = terminalDeleteRequests.length
  await detachPaneTerminal(page, 3, terminalId)
  await waitForPaneTabCount(page, 3, 1)
  if (await page.getByRole('dialog').count()) fail('pane detach opened a confirmation dialog')
  if (terminalDeleteRequests.length !== deleteCountBeforeDetach) {
    fail('pane X issued a terminal DELETE request')
  }
  if (!(await backendHasSessions(page, [terminalId]))) {
    fail('pane X terminated the detached backend session')
  }

  // Pin C into the empty P1, then prove focus and responsive modes are only
  // projections of the complete three-pane tree.
  await focusEmptyPane(page, 1)
  await ensureLocalGroupExpanded(page)
  await groupSessionItem(page, activePaneTerminalId).getByRole('button', { name: /^固定 本机 / }).click()
  await waitForPaneTabCount(page, 1, 1)
  await waitForTabMode(page, 1, activePaneTerminalId, 'pinned')
  await activatePaneTab(page, 1, activePaneTerminalId)
  const beforeFocusMode = await paneSnapshot(page)
  await page.getByRole('button', { name: '聚焦当前终端', exact: true }).click()
  await waitForPaneCount(page, 1)
  await assertPaneTablists(page, 1)
  let projected = await paneSnapshot(page)
  if (
    projected[0]?.number !== 1 ||
    JSON.stringify(projected[0]?.tabs) !== JSON.stringify([activePaneTerminalId]) ||
    projected[0]?.modes[activePaneTerminalId] !== 'pinned'
  ) {
    fail(`focus mode did not project P1 exactly: ${JSON.stringify(projected)}`)
  }
  await page.getByRole('button', { name: '恢复分屏', exact: true }).click()
  await waitForPaneCount(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeFocusMode)) {
    fail('restoring focus mode mutated pane state')
  }

  await page.setViewportSize({ width: 390, height: 844 })
  await waitForPaneCount(page, 1)
  await assertPaneTablists(page, 1)
  projected = await paneSnapshot(page)
  if (
    projected[0]?.number !== 1 ||
    JSON.stringify(projected[0]?.tabs) !== JSON.stringify([activePaneTerminalId])
  ) {
    fail(`mobile mode lost the focused P1: ${JSON.stringify(projected)}`)
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await waitForPaneCount(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeFocusMode)) {
    fail('leaving mobile mode mutated desktop pane state')
  }

  // Keyboard Delete has the same detach-only contract. B is P3's final tab,
  // so its editor group must remain empty with the same leaf identity.
  await activatePaneTab(page, 3, switchTerminalId)
  const p3LeafId = paneFromSnapshot(await paneSnapshot(page), 3).leafId
  const deleteCountBeforeKeyboardDetach = terminalDeleteRequests.length
  const bPaneTab = page.locator(
    `[data-terminal-pane-number="3"] [role="tab"][data-terminal-tab-id="${switchTerminalId}"]`,
  )
  await bPaneTab.focus()
  await bPaneTab.press('Delete')
  await waitForPaneTabCount(page, 3, 0)
  snapshot = await paneSnapshot(page)
  if (
    !paneFromSnapshot(snapshot, 3).empty ||
    paneFromSnapshot(snapshot, 3).leafId !== p3LeafId ||
    paneFromSnapshot(snapshot, 3).active !== null
  ) {
    fail(`keyboard detach collapsed or replaced empty P3: ${JSON.stringify(snapshot)}`)
  }
  if (await page.getByRole('dialog').count()) fail('keyboard detach opened a confirmation dialog')
  if (terminalDeleteRequests.length !== deleteCountBeforeKeyboardDetach) {
    fail('keyboard Delete issued a terminal DELETE request')
  }
  if (!(await backendHasSessions(page, [switchTerminalId]))) {
    fail('keyboard Delete terminated B in the backend')
  }

  // The X in the expanded device group is the sole destructive entry point.
  // It must confirm first and issue exactly one backend DELETE after approval.
  await ensureLocalGroupExpanded(page)
  const deleteCountBeforeTerminate = terminalDeleteRequests.length
  await groupSessionItem(page, switchTerminalId).getByRole('button', {
    name: /^终止 本机 .* 后台会话$/,
  }).click()
  const terminateDialog = page.getByRole('dialog')
  await terminateDialog.waitFor()
  if (terminalDeleteRequests.length !== deleteCountBeforeTerminate) {
    fail('device-group X deleted before confirmation')
  }
  await terminateDialog.getByRole('button', { name: '终止后台终端', exact: true }).click()
  await page.waitForFunction((id) => (
    ![...document.querySelectorAll('[aria-label*="双击固定"]')]
      .some((node) => node.getAttribute('aria-label')?.includes(id.slice(0, 8)))
  ), switchTerminalId)
  if (terminalDeleteRequests.length !== deleteCountBeforeTerminate + 1) {
    fail('device-group termination did not issue exactly one DELETE request')
  }
  if (!(await backendLacksSessions(page, [switchTerminalId]))) {
    fail('confirmed device-group termination left B in the backend')
  }

  // A large backend pool remains searchable but unassigned. It must not add
  // pane tabs or mount one xterm/WebSocket for every backend process.
  const scaleTerminalIds = await page.evaluate(async ({ prefix, count }) => (
    Promise.all(Array.from({ length: count }, async (_, index) => {
      const response = await fetch('/api/terminals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: `${prefix}${index + 1}` }),
      })
      if (!response.ok) throw new Error(`failed to create scale terminal ${index + 1}: ${response.status}`)
      return (await response.json()).id
    }))
  ), { prefix: scalePrefix, count: 18 })
  if (scaleTerminalIds.length !== 18 || scaleTerminalIds.some((id) => !id)) {
    fail('scale terminal creation returned invalid ids')
  }
  const beforeScaleReload = await paneSnapshot(page)
  await page.reload({ waitUntil: 'networkidle' })
  await waitForPaneCount(page, 3)
  await assertPaneTablists(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeScaleReload)) {
    fail('scale sessions were automatically inserted into pane tabs')
  }
  const mountedTerminalCount = Number(
    await page.locator('[data-mounted-terminal-count]').getAttribute('data-mounted-terminal-count'),
  )
  if (!Number.isFinite(mountedTerminalCount) || mountedTerminalCount > 8) {
    fail(`hidden terminal cache mounted ${mountedTerminalCount} terminals, expected at most 8`)
  }
  const visibleIdsAtScale = await visibleTerminalIds(page)
  if (
    visibleIdsAtScale.length !== 1 ||
    visibleIdsAtScale[0] !== activePaneTerminalId
  ) {
    fail(`empty panes or scale sessions became visible: ${JSON.stringify(visibleIdsAtScale)}`)
  }

  // A long device/terminal catalog must remain a 33px strip aligned with the
  // search trigger. Its native scrollbar is visually hidden (so it consumes
  // no row height), while trackpad/mouse horizontal scrolling remains usable.
  await ensureLocalGroupExpanded(page)
  const deviceStripMetrics = await page.evaluate(() => {
    const search = document.querySelector('button[aria-label="搜索终端"]')
    const strip = document.querySelector('[role="list"][aria-label="终端设备组"]')
    if (!(search instanceof HTMLElement) || !(strip instanceof HTMLElement)) return null
    const searchRect = search.getBoundingClientRect()
    const stripRect = strip.getBoundingClientRect()
    const groupRects = [...strip.children]
      .filter((element) => element.getAttribute('role') === 'listitem')
      .map((element) => element.getBoundingClientRect())
    const style = getComputedStyle(strip)
    return {
      searchHeight: searchRect.height,
      searchCenterY: searchRect.top + searchRect.height / 2,
      stripHeight: stripRect.height,
      stripCenterY: stripRect.top + stripRect.height / 2,
      groupHeights: groupRects.map((rect) => rect.height),
      groupCenterYs: groupRects.map((rect) => rect.top + rect.height / 2),
      clientWidth: strip.clientWidth,
      scrollWidth: strip.scrollWidth,
      clientHeight: strip.clientHeight,
      offsetHeight: strip.offsetHeight,
      overflowX: style.overflowX,
      scrollbarWidth: style.scrollbarWidth,
    }
  })
  if (!deviceStripMetrics) fail('terminal device strip metrics are unavailable')
  if (
    Math.abs(deviceStripMetrics.searchHeight - deviceStripMetrics.stripHeight) > 1 ||
    Math.abs(deviceStripMetrics.searchCenterY - deviceStripMetrics.stripCenterY) > 1 ||
    deviceStripMetrics.groupHeights.some((height) => (
      Math.abs(height - deviceStripMetrics.searchHeight) > 1
    )) ||
    deviceStripMetrics.groupCenterYs.some((centerY) => (
      Math.abs(centerY - deviceStripMetrics.searchCenterY) > 1
    ))
  ) {
    fail(`terminal device strip is not aligned with search: ${JSON.stringify(deviceStripMetrics)}`)
  }
  if (
    deviceStripMetrics.scrollWidth <= deviceStripMetrics.clientWidth + 1 ||
    !['auto', 'scroll'].includes(deviceStripMetrics.overflowX) ||
    deviceStripMetrics.scrollbarWidth !== 'none' ||
    deviceStripMetrics.offsetHeight - deviceStripMetrics.clientHeight > 1
  ) {
    fail(`terminal device strip scrollbar consumes layout or cannot overflow: ${JSON.stringify(deviceStripMetrics)}`)
  }
  const deviceStrip = page.getByRole('list', { name: '终端设备组', exact: true })
  await deviceStrip.evaluate((element) => { element.scrollLeft = 0 })
  await deviceStrip.hover()
  await page.mouse.wheel(360, 0)
  await page.waitForFunction(() => (
    (document.querySelector('[role="list"][aria-label="终端设备组"]')?.scrollLeft || 0) > 0
  ))

  await page.getByRole('button', { name: '搜索终端', exact: true }).click()
  let searchDialog = page.getByRole('dialog')
  await searchDialog.getByRole('combobox').fill(scalePrefix)
  await page.waitForFunction((expected) => (
    document.querySelectorAll('[role="dialog"] [role="option"]').length === expected
  ), 18)
  const searchListMetrics = await searchDialog
    .getByRole('listbox', { name: '终端搜索结果' })
    .evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: getComputedStyle(element).overflowY,
    }))
  if (
    searchListMetrics.scrollHeight <= searchListMetrics.clientHeight ||
    !['auto', 'scroll'].includes(searchListMetrics.overflowY)
  ) {
    fail(`terminal search results are not scrollable: ${JSON.stringify(searchListMetrics)}`)
  }
  await page.keyboard.press('Escape')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '搜索终端', exact: true }).click()
  searchDialog = page.getByRole('dialog')
  await searchDialog.getByRole('combobox').fill(scalePrefix)
  await page.waitForFunction((expected) => (
    document.querySelectorAll('[role="dialog"] [role="option"]').length === expected
  ), 18)
  const mobileDialog = await searchDialog.boundingBox()
  if (!mobileDialog || mobileDialog.x < 0 || mobileDialog.x + mobileDialog.width > 390) {
    fail(`terminal search dialog overflows the mobile viewport: ${JSON.stringify(mobileDialog)}`)
  }
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 1280, height: 800 })
  await waitForPaneCount(page, 3)

  // Closing an empty pane is also layout-only and collapses only its nearest
  // split; it must not broaden the destructive device-group contract.
  const deleteCountBeforePaneClose = terminalDeleteRequests.length
  await page.getByRole('button', { name: '关闭窗格 2（保留终端）', exact: true }).click()
  await waitForPaneCount(page, 2)
  await assertPaneTablists(page, 2)
  if (terminalDeleteRequests.length !== deleteCountBeforePaneClose) {
    fail('closing an editor group issued a terminal DELETE request')
  }
  if (await page.getByRole('separator').count() !== 1) {
    fail('closing nested P2 did not collapse exactly its nearest split')
  }

  await page.waitForTimeout(500)
  if (pageErrors.length) fail(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    fail(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log('terminal input/switch recovery, empty-pane split, device preview/pin, pane-tab drag, detach-vs-delete, persistence, focus/mobile, cache, workspace, artifact, and image contracts passed')
} finally {
  await page.evaluate(async ({ ids, ownedNamePrefix }) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return
    const sessions = await response.json()
    const explicitIds = new Set(ids.filter(Boolean))
    const owned = sessions.filter((session) => (
      explicitIds.has(session.id) || (session.name || '').startsWith(ownedNamePrefix)
    ))
    await Promise.all(owned.map((session) => (
      fetch(`/api/terminals/${encodeURIComponent(session.id)}`, { method: 'DELETE' })
    )))
  }, {
    ids: [terminalId, switchTerminalId, activePaneTerminalId],
    ownedNamePrefix: contractName,
  }).catch(() => {})
  await browser.close()
}
