import { PassThrough } from 'stream'

import { Box, renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { AUDIO_DIRECTIVE_RE, INLINE_RE, Md, MEDIA_LINE_RE, stripInlineMarkup } from '../components/markdown.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const matches = (text: string) => [...text.matchAll(INLINE_RE)].map(m => m[0])
const BEL = String.fromCharCode(7)
const ESC = String.fromCharCode(27)
const CSI_RE = new RegExp(`${ESC}\\[[0-?]*[ -/]*[@-~]`, 'g')
const OSC_RE = new RegExp(`${ESC}\\][\\s\\S]*?(?:${BEL}|${ESC}\\\\)`, 'g')

const renderPlain = (node: React.ReactNode) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(node, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return output
    .replace(OSC_RE, '')
    .split('\n')
    .map(line => stripAnsi(line).replace(CSI_RE, '').trimEnd())
}

describe('INLINE_RE emphasis', () => {
  it('matches word-boundary italic/bold', () => {
    expect(matches('say _hi_ there')).toEqual(['_hi_'])
    expect(matches('very __bold move__ today')).toEqual(['__bold move__'])
    expect(matches('(_paren_) and [_bracket_]')).toEqual(['_paren_', '_bracket_'])
  })

  it('keeps intraword underscores literal', () => {
    const path = '/home/me/.hermes/cache/screenshots/browser_screenshot_ecc1c3feab.png'

    expect(matches(path)).toEqual([])
    expect(matches('snake_case_var and MY_CONST')).toEqual([])
    expect(matches('foo__bar__baz')).toEqual([])
  })

  it('keeps Python dunder identifiers literal', () => {
    expect(matches('if __name__ == "__main__":')).toEqual([])
    expect(matches('def __init__(self):')).toEqual([])
    expect(matches('print(__file__)')).toEqual([])
  })

  it('still matches asterisk emphasis intraword', () => {
    expect(matches('a*b*c')).toEqual(['*b*'])
    expect(matches('a**bold**c')).toEqual(['**bold**'])
  })

  it('matches short alphanumeric subscript (H~2~O, CO~2~, X~n~)', () => {
    expect(matches('H~2~O')).toEqual(['~2~'])
    expect(matches('CO~2~ levels')).toEqual(['~2~'])
    expect(matches('the X~n~ term')).toEqual(['~n~'])
  })

  it('ignores kaomoji-style ~! and ~? punctuation', () => {
    // Kimi / Qwen / GLM 会把这些作为装饰符输出，两个波浪号之间的
    // 整段内容曾经被折叠成一个暗淡的色块。
    expect(matches('Aww ~! Building step by step, I love it ~!')).toEqual([])
    expect(matches('cool ~? yeah ~?')).toEqual([])
    expect(matches('mixed ~! and ~? flow')).toEqual([])
  })

  it('ignores tilde spans that contain spaces or punctuation', () => {
    // 真正的下标不会包含空格；波浪号后接一串文字再接波浪号
    // 几乎总是口语化表达，匹配它会吞掉文本。
    expect(matches('hello ~good idea~ there')).toEqual([])
    expect(matches('x ~oh no!~ y')).toEqual([])
  })

  it('does not let strikethrough eat subscript', () => {
    expect(matches('~~strike~~ and H~2~O')).toEqual(['~~strike~~', '~2~'])
  })
})

describe('stripInlineMarkup', () => {
  it('strips word-boundary emphasis only', () => {
    expect(stripInlineMarkup('say _hi_ there')).toBe('say hi there')
    expect(stripInlineMarkup('browser_screenshot_ecc.png')).toBe('browser_screenshot_ecc.png')
    expect(stripInlineMarkup('__bold move__ and foo__bar__')).toBe('bold move and foo__bar__')
  })

  it('preserves Python dunder identifiers', () => {
    expect(stripInlineMarkup('if __name__ == "__main__":')).toBe('if __name__ == "__main__":')
    expect(stripInlineMarkup('class X: def __init__(self): pass')).toBe('class X: def __init__(self): pass')
  })

  it('leaves ~!/~? kaomoji alone and still handles real subscript', () => {
    expect(stripInlineMarkup('Yay ~! nice work ~!')).toBe('Yay ~! nice work ~!')
    expect(stripInlineMarkup('H~2~O and CO~2~')).toBe('H_2O and CO_2')
  })

  it('strips inline math delimiters but keeps the formula text', () => {
    expect(stripInlineMarkup('$\\mathbb{Z}$ is a ring')).toBe('\\mathbb{Z} is a ring')
    expect(stripInlineMarkup('see \\(a + b\\) ok')).toBe('see a + b ok')
  })
})

describe('INLINE_RE inline math', () => {
  it('matches single-dollar math and beats emphasis at the same start', () => {
    // 如果没有数学公式处理，`*b*` 会被匹配为斜体，
    // 从而破坏公式。在 INLINE_RE 中加入数学公式后，第 0 列
    // 的最左匹配（`$P=a*b*c$`）会胜出。
    expect(matches('$P=a*b*c$')).toEqual(['$P=a*b*c$'])
    expect(matches('see $\\mathbb{Z}$ here')).toEqual(['$\\mathbb{Z}$'])
  })

  it('does not match currency-style prose', () => {
    expect(matches('it costs $5 and $10')).toEqual([])
    expect(matches('paid $5')).toEqual([])
  })

  it('does not let inline math swallow a $$ display fence', () => {
    // `$$x$$` 是一个 display block，而不是两个相邻的 inline math span。
    expect(matches('$$x$$')).toEqual([])
  })

  it('matches \\(...\\) inline math', () => {
    expect(matches('foo \\(x + y\\) bar')).toEqual(['\\(x + y\\)'])
  })

  it('does not corrupt subscripts/superscripts inside math', () => {
    // `_n` 和 `^r` 在普通文本中是 markdown 的强调/上标标记，
    // 但在 `$...$` span 内部，整个公式会被捕获为单个
    // inline math token，因此内部的正则永远看不到这些字符。
    expect(matches('$P=a_n x^n + a_0$')).toEqual(['$P=a_n x^n + a_0$'])
    expect(matches('$\\beta_1,\\dots,\\beta_r$')).toEqual(['$\\beta_1,\\dots,\\beta_r$'])
  })

  it('places math content in the correct capture group (regression: m[16] is bare URL)', () => {
    // 当 `m[16]` 同时是裸 URL 分组和 inline math `$...$`
    // 分组时（因为裸 URL 模式缺少自己的捕获括号），
    // MdInline 会把 `$\\mathbb{R}$` 渲染成带下划线的自动链接，
    // 而不是斜体琥珀色的数学公式。锁定编号：数学公式
    // 在 m[17] / m[18]，URL 在 m[16]。
    const url = [...'see https://example.com here'.matchAll(INLINE_RE)][0]!
    const dollarMath = [...'$\\mathbb{R}$'.matchAll(INLINE_RE)][0]!
    const parenMath = [...'\\(\\pi\\)'.matchAll(INLINE_RE)][0]!

    expect(url[16]).toBe('https://example.com')
    expect(url[17]).toBeUndefined()
    expect(url[18]).toBeUndefined()

    expect(dollarMath[16]).toBeUndefined()
    expect(dollarMath[17]).toBe('\\mathbb{R}')
    expect(dollarMath[18]).toBeUndefined()

    expect(parenMath[16]).toBeUndefined()
    expect(parenMath[17]).toBeUndefined()
    expect(parenMath[18]).toBe('\\pi')
  })
})

describe('protocol sentinels', () => {
  it('captures MEDIA: paths with surrounding quotes or backticks', () => {
    expect('MEDIA:/tmp/a.png'.match(MEDIA_LINE_RE)?.[1]).toBe('/tmp/a.png')
    expect('  MEDIA: /home/me/.hermes/cache/screenshots/browser_screenshot_ecc.png  '.match(MEDIA_LINE_RE)?.[1]).toBe(
      '/home/me/.hermes/cache/screenshots/browser_screenshot_ecc.png'
    )
    expect('`MEDIA:/tmp/a.png`'.match(MEDIA_LINE_RE)?.[1]).toBe('/tmp/a.png')
    expect('"MEDIA:C:\\files\\a.png"'.match(MEDIA_LINE_RE)?.[1]).toBe('C:\\files\\a.png')
  })

  it('ignores MEDIA: tokens embedded in prose', () => {
    expect('here is MEDIA:/tmp/a.png for you'.match(MEDIA_LINE_RE)).toBeNull()
    expect('the media: section is empty'.match(MEDIA_LINE_RE)).toBeNull()
  })

  it('matches the [[audio_as_voice]] directive', () => {
    expect(AUDIO_DIRECTIVE_RE.test('[[audio_as_voice]]')).toBe(true)
    expect(AUDIO_DIRECTIVE_RE.test('  [[audio_as_voice]]  ')).toBe(true)
    expect(AUDIO_DIRECTIVE_RE.test('audio_as_voice')).toBe(false)
  })
})

describe('Md wrapping', () => {
  it('trims spaces from word-wrap continuation lines', () => {
    const lines = renderPlain(
      React.createElement(Box, { width: 5 }, React.createElement(Md, { t: DEFAULT_THEME, text: 'Let me' }))
    )

    expect(lines).toContain('Let')
    expect(lines).toContain('me')
    expect(lines).not.toContain(' me')
  })

  it('keeps nested list and quote indentation out of trim-sensitive text', () => {
    const lines = renderPlain(
      React.createElement(
        Box,
        { flexDirection: 'column', width: 24 },
        React.createElement(Md, { t: DEFAULT_THEME, text: '  - nested bullet' }),
        React.createElement(Md, { t: DEFAULT_THEME, text: '>> nested quote' })
      )
    )

    expect(lines).toContain('  • nested bullet')
    expect(lines).toContain('  │ nested quote')
  })

  it('preserves original inline-code edge spaces', () => {
    const lines = renderPlain(
      React.createElement(Box, { width: 24 }, React.createElement(Md, { t: DEFAULT_THEME, text: '` hi ` ok' }))
    )

    expect(lines.some(line => line.startsWith(' hi  ok'))).toBe(true)
  })

  it('renders Python dunder identifiers literally outside code fences', () => {
    const lines = renderPlain(
      React.createElement(
        Box,
        { width: 80 },
        React.createElement(Md, {
          t: DEFAULT_THEME,
          text: 'if __name__ == "__main__":\n    obj.__init__()'
        })
      )
    )

    const rendered = lines.join('\n')

    expect(rendered).toContain('if __name__ == "__main__":')
    expect(rendered).toContain('obj.__init__()')
  })
})

describe('Md link labels', () => {
  it('renders bare URLs with readable slug labels', () => {
    const lines = renderPlain(
      React.createElement(
        Box,
        { width: 120 },
        React.createElement(Md, {
          t: DEFAULT_THEME,
          text: 'see https://www.expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure for details'
        })
      )
    )

    const rendered = lines.join('\n')

    expect(rendered).toContain('Puerto Rico El Yunque Rainforest Adventure')
    expect(rendered).not.toContain('https://www.expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure')
  })

  it('keeps explicit markdown labels as the immediate fallback', () => {
    const lines = renderPlain(
      React.createElement(
        Box,
        { width: 80 },
        React.createElement(Md, {
          t: DEFAULT_THEME,
          text: '[Trip details](https://www.expedia.com/things-to-do/puerto-rico-el-yunque-rainforest-adventure)'
        })
      )
    )

    expect(lines.join('\n')).toContain('Trip details')
  })
})

describe('renderTable CJK width alignment', () => {
  it('column starts share the same display offset across CJK rows', async () => {
    const { stringWidth } = await import('@hermes/ink')

    const md = [
      '| 配置 | Config | 状态 |',
      '|------|--------|------|',
      '| Vicuna (report) | dense | × |',
      '| ChatGLM | chat | ✓ |',
      '| 通义千问 | qwen | × |'
    ].join('\n')

    // 修复前的 bug：` `.repeat(w - stripInlineMarkup(...).length) 使用的是
    // UTF-16 代码单元，因此一个 CJK 表头单元格填充到 2 个显示宽度，
    // 而 body 单元格填充到 4 个，导致后续列每个 CJK 字符偏移 2 个
    // 显示宽度。
    //
    // 修复后的约定：列 N 开始之前的前缀，在表头和所有 body 行
    // 中具有相同的显示宽度（去重后跳过分隔行，因为它独立渲染）。
    const lines = renderPlain(
      React.createElement(Box, null, React.createElement(Md, { compact: true, t: DEFAULT_THEME, text: md }))
    ).filter(line => line.trim().length > 0)

    // 启发式规则：一行"数据行"要么包含 'Config'（表头），
    // 要么包含某个 body 标签；分隔行全是 box-drawing 字符。
    // 使用子串 'Config' / 'dense' / 'chat' / 'qwen' 作为
    // 每行第 2 列起始位置的唯一锚点。
    const colStarts = (line: string, anchor: string): number => {
      const idx = line.indexOf(anchor)

      return idx < 0 ? -1 : stringWidth(line.slice(0, idx))
    }

    const headerCol2 = lines.map(l => colStarts(l, 'Config')).find(v => v >= 0)
    const denseCol2 = lines.map(l => colStarts(l, 'dense')).find(v => v >= 0)
    const chatCol2 = lines.map(l => colStarts(l, 'chat')).find(v => v >= 0)
    const qwenCol2 = lines.map(l => colStarts(l, 'qwen')).find(v => v >= 0)

    expect(headerCol2).toBeDefined()
    expect(denseCol2).toBe(headerCol2)
    expect(chatCol2).toBe(headerCol2)
    // CJK 行就是修复前发生偏移的那一行。现在它必须
    // 与其他行对齐。
    expect(qwenCol2).toBe(headerCol2)
  })
})
