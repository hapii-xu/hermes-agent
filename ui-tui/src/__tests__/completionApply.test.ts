import { describe, expect, it } from 'vitest'

import { applyCompletion, completionToApplyOnSubmit } from '../domain/slash.js'

describe('applyCompletion', () => {
  it('replaces from compReplace and drops the leading slash from the row', () => {
    // gateway 的 slash completer 返回纯命令名，
    // replace_from = 1（即前导 "/" 之后的位置）。
    expect(applyCompletion('/ex', 'exit', 1)).toBe('/exit')
  })

  it('keeps the leading slash when the row carries one and input does not', () => {
    expect(applyCompletion('ex', '/exit', 0)).toBe('/exit')
  })

  it('replaces an argument token after a space (subcommand completion)', () => {
    expect(applyCompletion('/cron ad', 'add', 6)).toBe('/cron add')
  })
})

describe('completionToApplyOnSubmit', () => {
  it('accepts a completion that finishes a partial command name', () => {
    // "/ex" -> "/exit"：发生了实际的 token 变化，因此 Enter 接受该补全。
    expect(completionToApplyOnSubmit('/ex', 'exit', 1)).toBe('/exit')
  })

  it('does NOT swallow Enter when the completion only adds a trailing space', () => {
    // 这就是 bug：当 "/exit" 已完整输入后，gateway 返回的命令
    // 带有尾部空格（"exit "），导致 classic-CLI 下拉菜单保持打开。
    // 在 TUI 中绝不能吞掉 Enter —— 命令已经完整，
    // Enter 应该执行提交。
    expect(completionToApplyOnSubmit('/exit', 'exit ', 1)).toBeNull()
  })

  it('does not swallow Enter when applying the row is a no-op', () => {
    expect(completionToApplyOnSubmit('/exit', 'exit', 1)).toBeNull()
  })

  it('still accepts a real argument completion (no trailing-space false positive)', () => {
    expect(completionToApplyOnSubmit('/cron ad', 'add', 6)).toBe('/cron add')
  })

  it('submits (no accept) once an argument is fully typed and only a space is added', () => {
    expect(completionToApplyOnSubmit('/cron add', 'add ', 6)).toBeNull()
  })

  it('returns null when there is no row text', () => {
    expect(completionToApplyOnSubmit('/exit', undefined, 1)).toBeNull()
    expect(completionToApplyOnSubmit('/exit', '', 1)).toBeNull()
  })
})
