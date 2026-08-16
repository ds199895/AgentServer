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

async function paneSnapshot(page) {
  return page.locator('[data-terminal-pane-root="true"]').evaluateAll((nodes) => (
    nodes.map((node) => {
      const tabs = [...node.querySelectorAll('[role="tab"]')]
      return {
        number: Number(node.getAttribute('data-terminal-pane-number')),
        leafId: node.getAttribute('data-terminal-leaf-id'),
        empty: node.getAttribute('data-terminal-pane-empty') === 'true',
        tabs: tabs.map((tab) => tab.getAttribute('data-terminal-tab-id')),
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
    const root = panes.nth(index)
    const nestedTablists = await root.getByRole('tablist').count()
    if (nestedTablists !== 1) {
      fail(`pane frame ${index + 1} contains ${nestedTablists} tablists instead of one`)
    }
  }
}

async function waitForPaneTabCount(page, paneNumber, expected) {
  await page.waitForFunction(({ number, count }) => {
    const pane = document.querySelector(`[data-terminal-pane-number="${number}"]`)
    return pane?.querySelectorAll('[role="tab"]').length === count
  }, { number: paneNumber, count: expected })
}

async function activatePaneTab(page, paneNumber, sessionId) {
  const tab = page.locator(
    `[data-terminal-pane-number="${paneNumber}"] [data-terminal-tab-id="${sessionId}"]`,
  )
  await tab.click()
  await page.waitForFunction(({ number, id }) => {
    const pane = document.querySelector(`[data-terminal-pane-number="${number}"]`)
    return pane?.querySelector(`[data-terminal-tab-id="${id}"]`)
      ?.getAttribute('aria-selected') === 'true'
  }, { number: paneNumber, id: sessionId })
}

async function fillSearchAndChoose(page, query) {
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  const search = dialog.getByRole('combobox', { name: '搜索设备、终端、工作区或服务' })
  await search.fill(query)
  await dialog.getByRole('option').waitFor()
  await page.waitForFunction(() => {
    const combobox = document.querySelector('[role="dialog"] [role="combobox"]')
    const activeId = combobox?.getAttribute('aria-activedescendant')
    return Boolean(activeId && document.getElementById(activeId))
  })
  await search.press('Enter')
  await dialog.waitFor({ state: 'hidden' })
}

async function chooseFromGlobalSearch(page, query) {
  await page.getByRole('button', { name: '搜索终端', exact: true }).click()
  await fillSearchAndChoose(page, query)
}

async function closePaneTerminal(page, paneNumber, sessionId) {
  const pane = page.locator(`[data-terminal-pane-number="${paneNumber}"]`)
  const tab = pane.locator(`[data-terminal-tab-id="${sessionId}"]`)
  const tabWrapper = tab.locator('..')
  await tabWrapper.getByRole('button', { name: new RegExp(`^关闭窗格 ${paneNumber} 的终端 `) }).click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  await dialog.getByRole('button', { name: '关闭终端', exact: true }).click()
  await page.waitForFunction((id) => !document.querySelector(`[data-terminal-tab-id="${id}"]`), sessionId)
}

async function visibleTerminalIds(page) {
  return page.locator('[data-terminal-visible="true"]').evaluateAll((nodes) => (
    nodes.map((node) => node.getAttribute('data-terminal-id')).filter(Boolean).sort()
  ))
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

const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
})
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const pageErrors = []
const consoleErrors = []
const terminalDeleteRequests = []
const contractName = `contract-check-${Date.now().toString(36)}`
const switchName = `${contractName}-switch`
const unassignedName = `${contractName}-unassigned`
const scalePrefix = `${contractName}-scale-`
let terminalId = ''
let switchTerminalId = ''
let unassignedTerminalId = ''

page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text())
})
page.on('request', (request) => {
  if (request.method() === 'DELETE' && request.url().includes('/api/terminals/')) {
    terminalDeleteRequests.push(request.url())
  }
})

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '进入控制台' }).waitFor()
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.getByRole('navigation', { name: '主导航' }).waitFor()

  terminalId = await page.evaluate(async (name) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (!response.ok) throw new Error(`failed to create terminal: ${response.status}`)
    const session = await response.json()
    if (!session.id) throw new Error('created terminal has no id')
    if (session.workspace?.available !== true) {
      throw new Error(`created terminal has no workspace: ${session.workspace?.error || 'unknown error'}`)
    }
    return session.id
  }, contractName)

  switchTerminalId = await page.evaluate(async (name) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (!response.ok) throw new Error(`failed to create switch target: ${response.status}`)
    const session = await response.json()
    if (!session.id) throw new Error('switch target has no id')
    return session.id
  }, switchName)

  // Keep the existing workspace/file/artifact/read-image backend contract in
  // the same end-to-end flow. Pane behavior must not regress these APIs.
  await page.evaluate(async (sessionId) => {
    const request = async (path, init) => {
      const response = await fetch(path, init)
      if (!response.ok) {
        const detail = await response.text()
        throw new Error(`${init?.method || 'GET'} ${path} failed: ${response.status} ${detail}`)
      }
      return response
    }

    const workspaceResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/workspace?path=`,
    )
    const workspace = await workspaceResponse.json()
    if (!Array.isArray(workspace.entries)) throw new Error('workspace entries are not an array')
    const readme = workspace.entries.find((entry) => entry.path === 'README.md')
    if (!readme || readme.kind !== 'file') throw new Error('README.md missing from terminal workspace')

    const resolveResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/files/resolve`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'README.md' }),
      },
    )
    const grant = await resolveResponse.json()
    if (!grant.id || grant.terminal_id !== sessionId) throw new Error('invalid file grant payload')
    if (grant.path !== 'README.md' || !grant.etag) throw new Error('file grant lost path or version')

    const contentResponse = await request(
      `/api/files/${encodeURIComponent(grant.id)}/content?terminal_id=${encodeURIComponent(sessionId)}`,
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
      `/api/terminals/${encodeURIComponent(sessionId)}/artifacts`,
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

    const artifactsResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/artifacts`,
    )
    const artifacts = await artifactsResponse.json()
    const persisted = Array.isArray(artifacts)
      ? artifacts.find((event) => event.id === createdArtifact.id)
      : null
    if (!persisted || persisted.source !== 'e2e-contract' || persisted.schema_version !== 1) {
      throw new Error('created artifact was not present in the durable snapshot')
    }

    const readImageResponse = await request(
      `/api/terminals/${encodeURIComponent(sessionId)}/read-image`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'web_dist/favicon.png' }),
      },
    )
    const imageResult = await readImageResponse.json()
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
  }, terminalId)

  // Compatibility guard: old agents may omit services, but the UI must still
  // be able to construct every pane and its tab strip.
  await page.route('**/api/terminals', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }
    const response = await route.fetch()
    const sessions = await response.json()
    for (const session of sessions) delete session.services
    await route.fulfill({ response, json: sessions })
  })

  // A route opens only that terminal into P1. Other backend sessions remain
  // unassigned until the user chooses them; server polling must not flood P1.
  await page.goto(`${base}/terminal/${encodeURIComponent(terminalId)}`, { waitUntil: 'networkidle' })
  await waitForPaneCount(page, 1)
  await page.locator(`[data-terminal-id="${terminalId}"][data-terminal-visible="true"]`).waitFor()
  await assertPaneTablists(page, 1)

  const deviceGroups = page.getByRole('list', { name: '终端设备组' })
  await deviceGroups.waitFor()
  if (await deviceGroups.getByRole('tab').count() !== 0) {
    fail('global device navigation still exposes session tabs')
  }
  if (await deviceGroups.getByRole('listitem').count() < 1) {
    fail('global terminal navigation has no device/local group card')
  }
  let snapshot = await paneSnapshot(page)
  if (JSON.stringify(paneFromSnapshot(snapshot, 1).tabs) !== JSON.stringify([terminalId])) {
    fail(`P1 did not start with only the routed terminal: ${JSON.stringify(snapshot)}`)
  }

  // Global search attaches an unassigned backend session to the focused P1.
  await chooseFromGlobalSearch(page, switchTerminalId)
  await waitForPaneTabCount(page, 1, 2)
  snapshot = await paneSnapshot(page)
  const initialP1LeafId = paneFromSnapshot(snapshot, 1).leafId
  if (
    JSON.stringify(paneFromSnapshot(snapshot, 1).tabs) !== JSON.stringify([terminalId, switchTerminalId]) ||
    paneFromSnapshot(snapshot, 1).active !== switchTerminalId
  ) {
    fail(`P1 did not receive and activate its second local tab: ${JSON.stringify(snapshot)}`)
  }
  await activatePaneTab(page, 1, terminalId)
  await activatePaneTab(page, 1, switchTerminalId)
  if (paneFromSnapshot(await paneSnapshot(page), 1).active !== switchTerminalId) {
    fail('P1 pane-local switching did not update its own active tab')
  }
  await activatePaneTab(page, 1, terminalId)

  // Split P1 to the right. The new P2 owns its own tab list and its + action
  // appends to P2 without changing P1's selected tab.
  await page.getByRole('button', {
    name: '从窗格 1 新建同设备终端并向右分屏',
    exact: true,
  }).click()
  await waitForPaneCount(page, 2)
  await assertPaneTablists(page, 2)
  snapshot = await paneSnapshot(page)
  const p1AfterFirstSplit = paneFromSnapshot(snapshot, 1)
  const p2AfterFirstSplit = paneFromSnapshot(snapshot, 2)
  if (p1AfterFirstSplit.active !== terminalId || p1AfterFirstSplit.tabs.length !== 2) {
    fail(`splitting P1 changed its tabs or selection: ${JSON.stringify(snapshot)}`)
  }
  if (p2AfterFirstSplit.tabs.length !== 1 || !p2AfterFirstSplit.active) {
    fail(`new P2 did not contain exactly one active tab: ${JSON.stringify(snapshot)}`)
  }
  const p2LeafId = p2AfterFirstSplit.leafId
  const p2FirstTerminalId = p2AfterFirstSplit.active

  await page.getByRole('button', { name: '在窗格 2 新建终端标签', exact: true }).click()
  await waitForPaneTabCount(page, 2, 2)
  snapshot = await paneSnapshot(page)
  const p2AfterNewTab = paneFromSnapshot(snapshot, 2)
  const p2SecondTerminalId = p2AfterNewTab.active
  if (!p2SecondTerminalId || p2SecondTerminalId === p2FirstTerminalId) {
    fail(`P2 + action did not create and activate a second tab: ${JSON.stringify(snapshot)}`)
  }
  if (paneFromSnapshot(snapshot, 1).active !== terminalId) {
    fail('creating a P2 tab changed P1 selection')
  }
  await activatePaneTab(page, 2, p2FirstTerminalId)
  if (paneFromSnapshot(await paneSnapshot(page), 1).active !== terminalId) {
    fail('switching to P2 first tab changed P1 selection')
  }
  await activatePaneTab(page, 2, p2SecondTerminalId)
  if (paneFromSnapshot(await paneSnapshot(page), 1).active !== terminalId) {
    fail('switching to P2 second tab changed P1 selection')
  }

  // Split downward from P2, then prove P3 has the same independent +/switch
  // semantics while both P1 and P2 retain their own active tabs.
  await page.getByRole('button', {
    name: '从窗格 2 新建同设备终端并向下分屏',
    exact: true,
  }).click()
  await waitForPaneCount(page, 3)
  await assertPaneTablists(page, 3)
  snapshot = await paneSnapshot(page)
  const p3AfterSplit = paneFromSnapshot(snapshot, 3)
  const p3LeafId = p3AfterSplit.leafId
  const p3FirstTerminalId = p3AfterSplit.active
  if (!p3FirstTerminalId || p3AfterSplit.tabs.length !== 1) {
    fail(`new P3 did not contain exactly one active tab: ${JSON.stringify(snapshot)}`)
  }
  if (
    paneFromSnapshot(snapshot, 1).active !== terminalId ||
    paneFromSnapshot(snapshot, 2).active !== p2SecondTerminalId
  ) {
    fail(`splitting P2 changed an existing pane selection: ${JSON.stringify(snapshot)}`)
  }

  await page.getByRole('button', { name: '在窗格 3 新建终端标签', exact: true }).click()
  await waitForPaneTabCount(page, 3, 2)
  snapshot = await paneSnapshot(page)
  const p3SecondTerminalId = paneFromSnapshot(snapshot, 3).active
  if (!p3SecondTerminalId || p3SecondTerminalId === p3FirstTerminalId) {
    fail(`P3 + action did not create a second tab: ${JSON.stringify(snapshot)}`)
  }
  await activatePaneTab(page, 3, p3FirstTerminalId)
  snapshot = await paneSnapshot(page)
  if (
    paneFromSnapshot(snapshot, 1).active !== terminalId ||
    paneFromSnapshot(snapshot, 2).active !== p2SecondTerminalId
  ) {
    fail('switching P3 first tab changed P1 or P2')
  }
  await activatePaneTab(page, 3, p3SecondTerminalId)
  snapshot = await paneSnapshot(page)
  if (
    paneFromSnapshot(snapshot, 1).active !== terminalId ||
    paneFromSnapshot(snapshot, 2).active !== p2SecondTerminalId
  ) {
    fail('switching P3 second tab changed P1 or P2')
  }

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

  // Persist exact leaf membership and each leaf's selected tab across reload.
  const persistedThreePaneSnapshot = await paneSnapshot(page)
  await page.reload({ waitUntil: 'networkidle' })
  await waitForPaneCount(page, 3)
  await assertPaneTablists(page, 3)
  snapshot = await paneSnapshot(page)
  if (JSON.stringify(snapshot) !== JSON.stringify(persistedThreePaneSnapshot)) {
    fail(`pane membership/selection changed on reload: ${JSON.stringify({ before: persistedThreePaneSnapshot, after: snapshot })}`)
  }
  if (await deviceGroups.getByRole('tab').count() !== 0) {
    fail('reload reintroduced session tabs into the device-group navigation')
  }

  // Revealing an already assigned hidden P1 tab focuses its original leaf. It
  // must not be moved into the currently focused P3.
  const membershipBeforeReveal = (await paneSnapshot(page)).map(({ number, leafId, tabs }) => ({ number, leafId, tabs }))
  await chooseFromGlobalSearch(page, switchTerminalId)
  await page.locator(`[data-terminal-id="${switchTerminalId}"][data-terminal-focused="true"]`).waitFor()
  snapshot = await paneSnapshot(page)
  const membershipAfterReveal = snapshot.map(({ number, leafId, tabs }) => ({ number, leafId, tabs }))
  if (JSON.stringify(membershipAfterReveal) !== JSON.stringify(membershipBeforeReveal)) {
    fail(`global reveal moved an assigned tab: ${JSON.stringify({ membershipBeforeReveal, membershipAfterReveal })}`)
  }
  if (
    paneFromSnapshot(snapshot, 1).leafId !== initialP1LeafId ||
    paneFromSnapshot(snapshot, 1).active !== switchTerminalId ||
    paneFromSnapshot(snapshot, 2).active !== p2SecondTerminalId ||
    paneFromSnapshot(snapshot, 3).active !== p3SecondTerminalId
  ) {
    fail(`assigned reveal did not stay independent per leaf: ${JSON.stringify(snapshot)}`)
  }

  // A newly discovered backend session stays unassigned after reconciliation.
  // Global search attaches it to the leaf focused when the search was invoked.
  await activatePaneTab(page, 2, p2SecondTerminalId)
  unassignedTerminalId = await page.evaluate(async (name) => {
    const response = await fetch('/api/terminals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    if (!response.ok) throw new Error(`failed to create unassigned terminal: ${response.status}`)
    return (await response.json()).id
  }, unassignedName)
  const beforeUnassignedReload = await paneSnapshot(page)
  await page.reload({ waitUntil: 'networkidle' })
  await waitForPaneCount(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeUnassignedReload)) {
    fail('server reconciliation auto-inserted an unassigned session into a pane')
  }
  await chooseFromGlobalSearch(page, unassignedTerminalId)
  await waitForPaneTabCount(page, 2, 3)
  snapshot = await paneSnapshot(page)
  if (
    paneFromSnapshot(snapshot, 2).active !== unassignedTerminalId ||
    !paneFromSnapshot(snapshot, 2).tabs.includes(unassignedTerminalId) ||
    paneFromSnapshot(snapshot, 1).tabs.length !== 2 ||
    paneFromSnapshot(snapshot, 3).tabs.length !== 2
  ) {
    fail(`unassigned session was not attached only to focused P2: ${JSON.stringify(snapshot)}`)
  }

  // Workspace affordances still belong to the active terminal within a pane.
  await chooseFromGlobalSearch(page, terminalId)
  const primaryPane = page.locator(`[data-terminal-id="${terminalId}"]`)
  await primaryPane.getByRole('button', { name: '打开工作区文件' }).click()
  const workspacePane = page.getByRole('complementary', { name: /工作区/ })
  await workspacePane.waitFor()
  await workspacePane.getByRole('button', { name: /README\.md/ }).click()
  await workspacePane.locator('pre').filter({ hasText: '# AgentServer' }).waitFor()
  await workspacePane.getByRole('button', { name: '关闭文件预览' }).click()
  await workspacePane.getByRole('button', { name: /web_dist/ }).click()
  await workspacePane.getByRole('button', { name: /favicon\.png/ }).click()
  await workspacePane.getByRole('img', { name: 'favicon.png' }).waitFor()
  await workspacePane.getByRole('button', { name: /Artifacts/ }).click()
  await workspacePane.getByText('不可变').first().waitFor()
  await workspacePane.getByRole('button', { name: '关闭工作区' }).click()

  // Focus and responsive single-pane modes are view projections only. They
  // show the focused leaf's complete tab list and restore the same tree.
  await activatePaneTab(page, 2, unassignedTerminalId)
  const beforeFocusMode = await paneSnapshot(page)
  await page.getByRole('button', { name: '聚焦当前终端', exact: true }).click()
  await waitForPaneCount(page, 1)
  await assertPaneTablists(page, 1)
  let projected = await paneSnapshot(page)
  if (
    projected[0]?.number !== 2 ||
    JSON.stringify(projected[0]?.tabs) !== JSON.stringify(paneFromSnapshot(beforeFocusMode, 2).tabs) ||
    projected[0]?.active !== unassignedTerminalId
  ) {
    fail(`focus mode did not project the complete focused P2: ${JSON.stringify(projected)}`)
  }
  await page.getByRole('button', { name: '恢复分屏', exact: true }).click()
  await waitForPaneCount(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeFocusMode)) {
    fail('restoring focus mode mutated the persisted pane layout')
  }

  await page.setViewportSize({ width: 390, height: 844 })
  await waitForPaneCount(page, 1)
  await assertPaneTablists(page, 1)
  projected = await paneSnapshot(page)
  if (
    projected[0]?.number !== 2 ||
    JSON.stringify(projected[0]?.tabs) !== JSON.stringify(paneFromSnapshot(beforeFocusMode, 2).tabs)
  ) {
    fail(`mobile mode lost the focused pane's local tabs: ${JSON.stringify(projected)}`)
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await waitForPaneCount(page, 3)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(beforeFocusMode)) {
    fail('leaving mobile mode mutated the desktop pane layout')
  }

  // A large backend session pool remains searchable but unassigned. It must
  // not add tabs to any pane or mount one xterm/WebSocket per session.
  await page.evaluate(async ({ prefix, count }) => {
    await Promise.all(Array.from({ length: count }, async (_, index) => {
      const response = await fetch('/api/terminals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: `${prefix}${index + 1}` }),
      })
      if (!response.ok) throw new Error(`failed to create scale terminal ${index + 1}: ${response.status}`)
    }))
  }, { prefix: scalePrefix, count: 18 })
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
  if (visibleIdsAtScale.length !== 3) {
    fail(`three-pane layout exposed ${visibleIdsAtScale.length} active terminals instead of three`)
  }

  await page.getByRole('button', { name: '搜索终端', exact: true }).click()
  let searchDialog = page.getByRole('dialog')
  await searchDialog.getByRole('combobox').fill(scalePrefix)
  await page.waitForFunction((expected) => (
    document.querySelectorAll('[role="dialog"] [role="option"]').length === expected
  ), 18)
  const searchListMetrics = await searchDialog.getByRole('listbox', { name: '终端搜索结果' }).evaluate((element) => ({
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

  // Closing both P3 tabs terminates those two backend sessions but preserves
  // the editor group itself as a focused, explicitly empty pane.
  await activatePaneTab(page, 3, p3SecondTerminalId)
  const deleteCountBeforeTabClose = terminalDeleteRequests.length
  await closePaneTerminal(page, 3, p3FirstTerminalId)
  await waitForPaneTabCount(page, 3, 1)
  await closePaneTerminal(page, 3, p3SecondTerminalId)
  await waitForPaneTabCount(page, 3, 0)
  await assertPaneTablists(page, 3)
  snapshot = await paneSnapshot(page)
  const emptyP3 = paneFromSnapshot(snapshot, 3)
  if (!emptyP3.empty || emptyP3.active !== null || emptyP3.leafId !== p3LeafId) {
    fail(`closing P3's final tab collapsed or replaced the pane: ${JSON.stringify(snapshot)}`)
  }
  if (terminalDeleteRequests.length !== deleteCountBeforeTabClose + 2) {
    fail('closing two terminal tabs did not issue exactly two DELETE requests')
  }

  // Closing P2 is layout-only: all of its live sessions become unassigned,
  // the empty sibling survives, and its picker can explicitly reattach one.
  const p2SessionsBeforeClose = [...paneFromSnapshot(snapshot, 2).tabs]
  const deleteCountBeforePaneClose = terminalDeleteRequests.length
  await page.getByRole('button', { name: '关闭窗格 2（保留终端）', exact: true }).click()
  await waitForPaneCount(page, 2)
  await assertPaneTablists(page, 2)
  if (terminalDeleteRequests.length !== deleteCountBeforePaneClose) {
    fail('closing a pane issued a terminal DELETE request')
  }
  if (!(await backendHasSessions(page, p2SessionsBeforeClose))) {
    fail('closing P2 terminated one or more backend sessions')
  }
  for (const sessionId of p2SessionsBeforeClose) {
    if (await page.locator(`[data-terminal-tab-id="${sessionId}"]`).count()) {
      fail(`closed-pane session ${sessionId} remained assigned to another pane`)
    }
  }
  snapshot = await paneSnapshot(page)
  const receiverPane = paneFromSnapshot(snapshot, 2)
  if (!receiverPane.empty || receiverPane.leafId !== p3LeafId || receiverPane.tabs.length !== 0) {
    fail(`empty sibling pane did not survive P2 close: ${JSON.stringify(snapshot)}`)
  }
  if (await page.getByRole('separator').count() !== 1) {
    fail('closing nested P2 did not collapse exactly its nearest split')
  }

  await page.getByRole('button', { name: '向窗格 2 添加终端', exact: true }).click()
  await page.getByRole('dialog').getByText('向窗格 2 添加终端', { exact: true }).waitFor()
  await fillSearchAndChoose(page, p2FirstTerminalId)
  await waitForPaneTabCount(page, 2, 1)
  snapshot = await paneSnapshot(page)
  if (
    paneFromSnapshot(snapshot, 2).leafId !== p3LeafId ||
    JSON.stringify(paneFromSnapshot(snapshot, 2).tabs) !== JSON.stringify([p2FirstTerminalId]) ||
    paneFromSnapshot(snapshot, 2).active !== p2FirstTerminalId
  ) {
    fail(`empty-pane picker did not reattach the chosen unassigned session: ${JSON.stringify(snapshot)}`)
  }
  if (await page.getByRole('separator').count() !== 1) {
    fail('reattaching an unassigned session unexpectedly created a split')
  }
  if (!p2LeafId || p2LeafId === p3LeafId) {
    fail('test setup did not create distinct P2 and P3 leaf identities')
  }

  const finalPersistedSnapshot = await paneSnapshot(page)
  await page.reload({ waitUntil: 'networkidle' })
  await waitForPaneCount(page, 2)
  await assertPaneTablists(page, 2)
  if (JSON.stringify(await paneSnapshot(page)) !== JSON.stringify(finalPersistedSnapshot)) {
    fail('reattached empty-pane membership did not persist across reload')
  }

  await page.waitForTimeout(500)
  if (pageErrors.length) fail(`page errors: ${pageErrors.join(' | ')}`)
  const meaningfulConsoleErrors = consoleErrors.filter(
    (item) => !item.includes('favicon') && !item.includes('401 (Unauthorized)'),
  )
  if (meaningfulConsoleErrors.length) {
    fail(`console errors: ${meaningfulConsoleErrors.join(' | ')}`)
  }
  console.log('VSCode-style independent pane tabs, persistence, reveal/attach, empty-pane, close-pane, responsive, cache, workspace, artifact, and image contracts passed')
} finally {
  await page.evaluate(async ({ sessionId, switchSessionId, unassignedSessionId, ownedNamePrefix }) => {
    const response = await fetch('/api/terminals')
    if (!response.ok) return
    const sessions = await response.json()
    const owned = sessions.filter((session) => (
      session.id === sessionId ||
      session.id === switchSessionId ||
      session.id === unassignedSessionId ||
      (session.name || '').startsWith(ownedNamePrefix)
    ))
    await Promise.all(owned.map((session) => (
      fetch(`/api/terminals/${encodeURIComponent(session.id)}`, { method: 'DELETE' })
    )))
  }, {
    sessionId: terminalId,
    switchSessionId: switchTerminalId,
    unassignedSessionId: unassignedTerminalId,
    ownedNamePrefix: contractName,
  }).catch(() => {})
  await browser.close()
}
