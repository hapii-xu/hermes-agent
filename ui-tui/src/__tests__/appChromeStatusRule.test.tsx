import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { DEFAULT_THEME } from '../theme.js'

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

const findClickableWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findClickableWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  if (typeof node.props.onClick === 'function' && textContent(node).includes(needle)) {
    return node
  }

  return findClickableWithText(node.props.children, needle)
}

// 找到最内层的元素，其自身（直接）文本内容包含
// needle。用于断言 notice 文本渲染时使用的颜色。
const findElementWithText = (node: ReactNodeLike, needle: string): React.ReactElement | null => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElementWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  // 优先使用最深处的匹配元素，这样我们获取的是实际携带颜色
  // 的叶子 <Text>，而不是祖先 Box。
  const deeper = findElementWithText(node.props.children, needle)

  if (deeper) {
    return deeper
  }

  return textContent(node).includes(needle) ? node : null
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

describe('StatusRule background-subagent indicator', () => {
  it('renders ⛓ N on a wide terminal when subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 3 }
    })

    expect(textContent(element)).toContain('⛓ 3')
  })

  it('omits the segment when no subagents are running', () => {
    const element = StatusRule({
      ...baseProps,
      usage: { ...baseProps.usage, active_subagents: 0 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('omits the segment when the field is absent', () => {
    const element = StatusRule({ ...baseProps })

    expect(textContent(element)).not.toContain('⛓')
  })

  it('drops the subagent segment before the bg segment on a narrow terminal', () => {
    // cols=44 低于 subagents 断点 (92)，也低于 bg 断点
    // (88) —— 两者都消失。断言当空间紧张时，即使有活跃计数，
    // 优先级较低的 subagent 指示器也不会显示。
    const element = StatusRule({
      ...baseProps,
      cols: 44,
      bgCount: 1,
      usage: { ...baseProps.usage, active_subagents: 2 }
    })

    expect(textContent(element)).not.toContain('⛓')
  })
})

describe('StatusRule session count click target', () => {
  it('makes the live session count itself clickable', () => {
    const openSwitcher = vi.fn()
    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 100,
      cwdLabel: '~/repo',
      liveSessionCount: 1,
      model: 'kimi-k2.6',
      onSessionCountClick: openSwitcher,
      sessionStartedAt: null,
      showCost: false,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: { total: 0 },
      voiceLabel: ''
    })

    const clickableSessionCount = findClickableWithText(element, '1 session')

    expect(clickableSessionCount).not.toBeNull()
    clickableSessionCount!.props.onClick({ stopImmediatePropagation: vi.fn() })
    expect(openSwitcher).toHaveBeenCalledOnce()
  })

  it('keeps status + model and drops the low-value tail on a narrow terminal', () => {
    const element = StatusRule({
      bgCount: 0,
      busy: false,
      cols: 44,
      cwdLabel: '~/src/hermes-agent/apps/desktop (bb/tui-statusbar-responsive)',
      liveSessionCount: 3,
      model: 'opus-4.8',
      onSessionCountClick: vi.fn(),
      sessionStartedAt: Date.now() - 60_000,
      showCost: true,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      turnStartedAt: null,
      usage: { context_max: 200_000, context_percent: 25, context_used: 50_000, cost_usd: 0.5, total: 50_000 },
      voiceLabel: 'voice off'
    })

    const rendered = textContent(element)

    // 必须保留的核心内容完整保留 …
    expect(rendered).toContain('ready')
    expect(rendered).toContain('opus 4.8')
    // … 而低价值的尾部（session 计数、费用）被丢弃，而非截断。
    expect(rendered).not.toContain('3 sessions')
    expect(rendered).not.toContain('$0.5000')
  })
})

describe('StatusRule credits notice render priority', () => {
  it('replaces the idle status with the notice text and keeps model + context', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ credits exhausted' }
    })

    const rendered = textContent(element)

    // Notice 替换了 status verb 槽位 …
    expect(rendered).toContain('✕ credits exhausted')
    expect(rendered).not.toContain('ready')
    // … 但 model + context 仍然可见。
    expect(rendered).toContain('opus 4.8')
    expect(rendered).toContain('50k')
  })

  it('busy wins: the FaceTicker shows, the notice is hidden mid-turn', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' },
      turnStartedAt: Date.now()
    })

    const rendered = textContent(element)

    // 忙碌时 notice 不能渲染。
    expect(rendered).not.toContain('⚠ 90% used')
    // Model 仍然可见。
    expect(rendered).toContain('opus 4.8')
  })

  it('colours the notice by level (error → theme error, success → statusGood)', () => {
    const errEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.depleted', kind: 'sticky', level: 'error', text: '✕ exhausted' }
    })
    const errText = findElementWithText(errEl, '✕ exhausted')
    expect(errText?.props.color).toBe(DEFAULT_THEME.color.error)

    const okEl = StatusRule({
      ...baseProps,
      notice: { key: 'credits.restored', kind: 'ttl', level: 'success', text: '✓ restored', ttl_ms: 8000 }
    })
    const okText = findElementWithText(okEl, '✓ restored')
    expect(okText?.props.color).toBe(DEFAULT_THEME.color.statusGood)
  })

  it('does NOT add a glyph — the notice text is rendered verbatim', () => {
    const element = StatusRule({
      ...baseProps,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: '⚠ 90% used' }
    })
    const noticeText = findElementWithText(element, '90% used')

    // 叶子元素恰好携带策略文本 —— 没有额外前置的图标。
    expect(noticeText?.props.children).toBe('⚠ 90% used')
  })

  it('the notice text is the shrinkable element (flexShrink=1 + truncate-end) so a long notice ellipsizes', () => {
    const longText = '⚠ ' + 'x'.repeat(200)
    const element = StatusRule({
      ...baseProps,
      cols: 50,
      notice: { key: 'credits.90', kind: 'sticky', level: 'warn', text: longText }
    })

    // 叶子 <Text> 使用截断而非换行/裁剪固定的尾部。
    const noticeText = findElementWithText(element, 'xxxxx')
    expect(noticeText?.props.wrap).toBe('truncate-end')

    // 其容器 box 优先让出空间 (flexShrink=1)，使 model 保持可见。
    const findShrinkBoxContaining = (node: ReactNodeLike): React.ReactElement | null => {
      if (!React.isValidElement(node)) {
        if (Array.isArray(node)) {
          for (const c of node) {
            const f = findShrinkBoxContaining(c)
            if (f) return f
          }
        }
        return null
      }
      if (node.props.flexShrink === 1 && textContent(node).includes('xxxxx') && node.type !== StatusRule) {
        // 优先使用包裹 notice 文本的最近 shrink box。
        const deeper = findShrinkBoxContaining(node.props.children)
        return deeper ?? node
      }
      return findShrinkBoxContaining(node.props.children)
    }
    const shrinkBox = findShrinkBoxContaining(element)
    expect(shrinkBox).not.toBeNull()

    // 在窄终端上 model 仍然保留，因为 notice 让出了空间。
    expect(textContent(element)).toContain('opus 4.8')
  })
})

describe('StatusRule idle-since read-out', () => {
  // IdleSince component 使用了 hooks，因此无法在
  // renderer 外部调用 —— 改为对 element 树进行断言（与 duration
  // 测试不检查 SessionDuration 文本的原因相同）。
  const findComponentByName = (node: ReactNodeLike, name: string): React.ReactElement | null => {
    if (node === null || node === undefined || typeof node === 'boolean') {
      return null
    }

    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findComponentByName(child, name)

        if (found) {
          return found
        }
      }

      return null
    }

    if (!React.isValidElement(node)) {
      return null
    }

    if (typeof node.type === 'function' && node.type.name === name) {
      return node
    }

    return findComponentByName(node.props.children, name)
  }

  it('shows time since the last final agent response when idle', () => {
    const endedAt = Date.now() - 42_000
    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: endedAt,
      sessionStartedAt: Date.now() - 60_000
    })

    const idle = findComponentByName(element, 'IdleSince')

    expect(idle).not.toBeNull()
    expect(idle!.props.endedAt).toBe(endedAt)
  })

  it('is hidden while a turn is busy', () => {
    const element = StatusRule({
      ...baseProps,
      busy: true,
      lastTurnEndedAt: Date.now() - 42_000,
      turnStartedAt: Date.now()
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })

  it('is hidden before the first turn completes', () => {
    const element = StatusRule({
      ...baseProps,
      lastTurnEndedAt: null,
      sessionStartedAt: Date.now() - 60_000
    })

    expect(findComponentByName(element, 'IdleSince')).toBeNull()
  })
})
