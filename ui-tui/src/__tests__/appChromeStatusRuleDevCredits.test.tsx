import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

// DEV_CREDITS_MODE 是一个模块加载时常量（config/env.ts 在 import 时
// 只读取一次 process.env.HERMES_DEV_CREDITS）。在测试内部修改 process.env
// 无法在模块加载后改变它的值 —— 因此在此文件中将该模块 mock 为
// dev-on 值。vitest 会将 vi.mock 提升到 import 之前，所以
// appChrome 会获取到 mock 后的标志。放在独立文件中是为了让 override
// 保持作用域隔离（其他 StatusRule 测试使用真实的 dev-off 值运行）。
vi.mock('../config/env.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../config/env.js')>()
  return { ...actual, DEV_CREDITS_MODE: true }
})

type ReactNodeLike = React.ReactNode

const textContent = (node: ReactNodeLike): string => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  if (React.isValidElement(node)) {
    return textContent(node.props.children)
  }

  return ''
}

const baseProps = {
  bgCount: 0,
  busy: false,
  cols: 100,
  cwdLabel: '~/repo',
  liveSessionCount: 0,
  model: 'opus-4.8',
  sessionStartedAt: null,
  showCost: false,
  status: 'ready',
  statusColor: DEFAULT_THEME.color.ok,
  t: DEFAULT_THEME,
  turnStartedAt: null,
  usage: { context_max: 200_000, context_percent: 25, context_used: 50_000, total: 50_000 },
  voiceLabel: ''
}

describe('StatusRule dev-credits banner (HERMES_DEV_CREDITS on)', () => {
  it('keeps the dev-credits banner visible alongside a notice', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' },
      usage: { ...baseProps.usage, dev_credits_spent_micros: 12_345 }
    })

    const rendered = textContent(element)

    // Notice 和 dev banner 共存 …
    expect(rendered).toContain('⚠ 90% used')
    expect(rendered).toContain('(dev credits)')
    // … 且 Δ spend 段渲染（12345 micros → 1.2¢）。
    expect(rendered).toContain('Δ')
  })
})
