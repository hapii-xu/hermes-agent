import { beforeEach, describe, expect, it, vi } from 'vitest'

import { creditsCommands } from '../app/slash/commands/credits.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import type { CreditsViewResponse } from '../gatewayTypes.js'

// 这个命令在确认时通过这个辅助函数打开充值 URL。Mock 它以便
// 测试不会调用真实的浏览器/`xdg-open`，我们可以确定性地断言
// 成功/失败消息。
vi.mock('../lib/openExternalUrl.js', () => ({
  openExternalUrl: vi.fn(() => true)
}))

import { openExternalUrl } from '../lib/openExternalUrl.js'

const openExternalUrlMock = vi.mocked(openExternalUrl)

const creditsCommand = creditsCommands.find(cmd => cmd.name === 'credits')!

const buildView = (overrides: Partial<CreditsViewResponse> = {}): CreditsViewResponse => ({
  balance_lines: ['Grant: $9.50 left', 'Top-up: $25.00'],
  depleted: false,
  identity_line: 'Signed in as ada@example.com',
  logged_in: true,
  topup_url: 'https://portal.nousresearch.com/billing/topup',
  ...overrides
})

// 模拟 createSlashHandler 的真实 `guarded` 包装器：当命令已过期
// 或响应为假值时跳过 handler。测试保持非过期状态，所以这是一个
// 简单的"收到响应时运行 handler"的 shim。
const guarded =
  <T,>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

const buildCtx = (rpcResult: CreditsViewResponse) => {
  const sys = vi.fn()
  const rpc = vi.fn(() => Promise.resolve(rpcResult))
  const guardedErr = vi.fn()

  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr,
    sid: 'sid-abc',
    stale: () => false,
    transcript: { page: vi.fn(), panel: vi.fn(), sys }
  }

  // 运行命令，然后 await rpc 的 promise，确保 .then() handler 在
  // 断言前已执行完毕——确定性，无需轮询/超时。
  const run = async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    creditsCommand.run('', ctx as any, 'credits')
    await rpc.mock.results[0]?.value
    // 允许链式的 .then() 微任务完成。
    await Promise.resolve()
  }

  return { ctx, rpc, run, sys }
}

describe('/credits slash command', () => {
  beforeEach(() => {
    resetOverlayState()
    openExternalUrlMock.mockClear()
    openExternalUrlMock.mockReturnValue(true)
  })

  it('renders the balance (including top-up URL) and arms the confirm overlay', async () => {
    const view = buildView()
    const { rpc, run, sys } = buildCtx(view)

    await run()

    expect(rpc).toHaveBeenCalledWith('credits.view', { session_id: 'sid-abc' })

    // (a) sys 收到了包含 topup_url 的余额文本
    const printed = sys.mock.calls.map(call => call[0]).join('\n')
    expect(printed).toContain('💳 Nous credits')
    expect(printed).toContain('Grant: $9.50 left')
    expect(printed).toContain('Signed in as ada@example.com')
    expect(printed).toContain(view.topup_url)

    // (b) 确认弹层已设置预期的 label + detail
    const confirm = getOverlayState().confirm
    expect(confirm).toBeTruthy()
    expect(confirm?.confirmLabel).toBe('Open top-up in browser')
    expect(confirm?.cancelLabel).toBe('Cancel')
    expect(confirm?.title).toBe('Add credits?')
    expect(confirm?.detail).toBe(view.topup_url)

    // onConfirm 打开 URL 并将成功消息报告回 transcript
    confirm?.onConfirm()
    expect(openExternalUrlMock).toHaveBeenCalledWith(view.topup_url)
    expect(sys).toHaveBeenCalledWith(
      'Complete your top-up in the browser — credits will appear in /credits shortly.'
    )
  })

  it('falls back to printing the URL when the browser open is rejected', async () => {
    openExternalUrlMock.mockReturnValue(false)
    const view = buildView()
    const { run, sys } = buildCtx(view)

    await run()

    const confirm = getOverlayState().confirm
    expect(confirm).toBeTruthy()
    confirm?.onConfirm()
    expect(sys).toHaveBeenCalledWith(`Open this URL to top up: ${view.topup_url}`)
  })

  it('does not arm the confirm overlay when there is no top-up URL', async () => {
    const view = buildView({ topup_url: null })
    const { run, sys } = buildCtx(view)

    await run()

    const printed = sys.mock.calls.map(call => call[0]).join('\n')
    expect(printed).toContain('💳 Nous credits')
    expect(getOverlayState().confirm).toBeNull()
  })

  it('shows the not-logged-in message and does NOT arm the confirm overlay', async () => {
    const view = buildView({
      balance_lines: [],
      identity_line: null,
      logged_in: false,
      topup_url: null
    })
    const { run, sys } = buildCtx(view)

    await run()

    expect(sys).toHaveBeenCalledWith('💳 Not logged into Nous Portal — run /portal to log in.')
    expect(getOverlayState().confirm).toBeNull()
    expect(openExternalUrlMock).not.toHaveBeenCalled()
  })
})
