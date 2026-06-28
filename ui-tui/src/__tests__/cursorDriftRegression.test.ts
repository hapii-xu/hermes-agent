/**
 * 多行 composer cursor 漂移 bug 的固定回归测试。
 *
 * 症状：在 `hermes --tui` 中，向 composer 输入内容直到文本
 * 换行成多行视觉行时，最后一个输入的字符与（硬件）cursor
 * 块之间会出现多个空白单元格。在窄终端上（尤其是 Cursor IDE
 * 内置终端）问题更为严重。
 *
 * 根本原因：composer 的 `cursorLayout`（由 `useDeclaredCursor`
 * 用来放置硬件 cursor）使用了一个手写的 word-wrap 算法，
 * 而 Ink 的 `<Text wrap="wrap">` 通过 `wrap-ansi` 进行渲染。两者
 * 在许多实际输入上不一致——wrap-ansi 会将 "branch
 * investigate" 保持在一行，而 cursorLayout 却认为它已经换行，
 * 等等——导致声明的 cursor 位置与实际文本渲染位置发生漂移。
 * 修复方案是让 cursorLayout 的行断点直接取自 wrap-ansi，
 * 从而保证两者一致。
 *
 * 此测试固定了这一契约：对于向 composer 输入的每一个字符，
 * cursorLayout 报告的 cursor 位置必须等于 wrap-ansi 渲染的
 * 文本末尾位置。任何让两者重新产生分歧的回归都会重新引入漂移。
 */
import { wrapAnsi } from '@hermes/ink'
import { describe, expect, it } from 'vitest'

import { cursorLayout, inputVisualHeight } from '../lib/inputMetrics.js'

function wrapAnsiEnd(text: string, cols: number): { line: number; column: number } {
  const wrapped = wrapAnsi(text, cols, { hard: true, trim: false })
  const lines = wrapped.split('\n')
  const last = lines[lines.length - 1] ?? ''

  return { line: lines.length - 1, column: last.length }
}

const USER_REPORT_MESSAGE =
  // 用户实际 bug 报告的改写，逐字包含以便测试
  // 基于真实的输入模式（长单行、混合长度的单词、
  // 标点符号、无硬换行）。
  'im in cursor terminal using hermes --tui and as i type multiline my caret at the end will often ' +
  'go.. randomly.. like multiple spaces away lol and idk why. theres no rhyme/reason really but ' +
  'there should literally never be a non-user added space at the end of my composer input right? ' +
  'i dont think it happens on new sessions but only existing ones. there have been a few prs to ' +
  'try to fix this and all not working. ok it just happened, to me, nowso attaching screenshot ' +
  'and you can see its multiline, new session. on a new bb/<xxx> branch investigate'

describe('cursor-drift regression — composer cursorLayout matches Ink rendering', () => {
  it('agrees with wrap-ansi at every typing-prefix of the user-reported message', () => {
    // 逐字符遍历消息（模拟 TUI 在用户输入时看到的内容）。
    // 在每个前缀处，cursorLayout 必须将 cursor 放置在
    // wrap-ansi 渲染文本末尾的确切位置。
    //
    // 修复前：在大多数窄宽度上会失败，因为手写的
    // wrap 算法在略不同于 wrap-ansi 的位置断行。
    for (const cols of [40, 50, 55, 60, 65, 70, 80]) {
      let acc = ''

      for (const ch of USER_REPORT_MESSAGE) {
        acc += ch
        const layout = cursorLayout(acc, acc.length, cols)
        const expected = wrapAnsiEnd(acc, cols)

        expect(
          layout,
          `mismatch at cols=${cols}, len=${acc.length}, last-char=${JSON.stringify(ch)}, ` +
            `tail=${JSON.stringify(acc.slice(-30))}`
        ).toEqual(expected)
      }
    }
  })

  it('keeps cursor on the same row when text exactly fills the terminal width', () => {
    // wrap-ansi 不会将刚好填满的文本推到下一行虚拟行。
    // 之前的算法会这样做——这就是在窄终端上某些消息长度时
    // 产生"cursor 停在最后一个字符下一行"可见症状的原因。
    for (const cols of [8, 12, 18, 24]) {
      const text = 'a'.repeat(cols)
      const layout = cursorLayout(text, text.length, cols)
      const inkLines = wrapAnsi(text, cols, { hard: true, trim: false }).split('\n')

      expect(layout.line).toBe(0)
      expect(layout.column).toBe(cols)
      expect(inkLines).toHaveLength(1)
      expect(inputVisualHeight(text, cols)).toBe(1)
    }
  })

  it('does not stuff a trailing whitespace word onto a phantom line', () => {
    // "branch investigate" 在 cols=20 时在 wrap-ansi 中适合一行。
    // bug 却声称 otherwise，将 cursor 停在 (line=1, col=?)，
    // 导致用户的 "branch investigate" 单独渲染在第 0 行，
    // 而 cursor 块在其后几个单元格处。
    const text = 'branch investigate'
    const cols = 20

    expect(cursorLayout(text, text.length, cols)).toEqual({ column: text.length, line: 0 })
    expect(cursorLayout(text, text.length, cols)).toEqual(wrapAnsiEnd(text, cols))
  })

  it('agrees with wrap-ansi for word-wrap that pushes a word onto the next line', () => {
    // "hello world" 在 cols=8 时在 wrap-ansi 中换行为 ["hello ", "world"]。
    // 文本末尾的 cursor 必须落在 line=1, col=5——即 Ink
    // 实际渲染最后一个 'd' 的位置。之前的算法在此报告
    // (line=2, col=0)（虚拟的额外换行），导致 cursor
    // 停在 Ink 从未绘制的一行上。
    const text = 'hello world'
    const cols = 8

    expect(cursorLayout(text, text.length, cols)).toEqual({ column: 5, line: 1 })
    expect(cursorLayout(text, text.length, cols)).toEqual(wrapAnsiEnd(text, cols))
  })
})
