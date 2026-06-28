import { afterEach, describe, expect, it, vi } from 'vitest'

const originalPlatform = process.platform

async function importPlatform(platform: NodeJS.Platform) {
  vi.resetModules()
  Object.defineProperty(process, 'platform', { value: platform })

  return import('../lib/platform.js')
}

afterEach(() => {
  Object.defineProperty(process, 'platform', { value: originalPlatform })
  vi.resetModules()
})

describe('platform action modifier', () => {
  it('treats kitty Cmd sequences as the macOS action modifier', async () => {
    const { isActionMod } = await importPlatform('darwin')

    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: true, super: false })).toBe(true)
    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(false)
  })

  it('still uses Ctrl as the action modifier on non-macOS', async () => {
    const { isActionMod } = await importPlatform('linux')

    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(false)
  })
})

describe('isCopyShortcut', () => {
  it('keeps Ctrl+C as the local non-macOS copy chord', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: false }, 'c', {})).toBe(true)
  })

  it('accepts client Cmd+C over SSH even when running on Linux', async () => {
    const { isCopyShortcut } = await importPlatform('linux')
    const env = { SSH_CONNECTION: '1 2 3 4' } as NodeJS.ProcessEnv

    expect(isCopyShortcut({ ctrl: false, meta: false, super: true }, 'c', env)).toBe(true)
    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', env)).toBe(true)
  })

  it('does not treat local Linux Alt+C as copy', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', {})).toBe(false)
  })

  it('accepts the VS Code/Cursor forwarded Cmd+C copy sequence on macOS', async () => {
    const { isCopyShortcut } = await importPlatform('darwin')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: true }, 'c', {})).toBe(true)
  })
})

describe('isVoiceToggleKey', () => {
  it('matches raw Ctrl+B on macOS (doc-default across platforms)', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'B')).toBe(true)
  })

  it('matches kitty-style Cmd+B on macOS via key.super', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b')).toBe(true)
    // ``key.meta`` 不被接受为 Cmd —— hermes-ink 使用 meta 表示
    // Alt，因此接受它会将 Alt+B 泄漏到默认绑定中
    //（#19835 的 Copilot 第 6 轮 review）。旧式终端的 Mac 用户
    // 使用严格的 Ctrl+B。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b')).toBe(false)
  })

  it('matches Ctrl+B on non-macOS platforms', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
  })

  it('does not match unmodified b or other Ctrl combos', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'b')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'a')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'c')).toBe(false)
  })
})

describe('parseVoiceRecordKey (#18994)', () => {
  it('falls back to Ctrl+B for empty input', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses ctrl+<letter> bindings', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('ctrl+o')).toEqual({ ch: 'o', mod: 'ctrl', raw: 'ctrl+o' })
    expect(parseVoiceRecordKey('Ctrl+R')).toEqual({ ch: 'r', mod: 'ctrl', raw: 'ctrl+r' })
  })

  it('parses alt/super aliases', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('alt+b').mod).toBe('alt')
    expect(parseVoiceRecordKey('option+b').mod).toBe('alt')
    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('win+b').mod).toBe('super')
  })

  it('treats ambiguous mac modifiers (meta / cmd / command) as unrecognised', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // ``meta`` / ``cmd`` / ``command`` 在传输中是有歧义的：
    // hermes-ink 在每个平台上为普通 Alt 设置 ``key.meta``，
    // 在旧式 macOS 终端上为 Cmd 也设置 ``key.meta``。接受其中任何一个
    // 都会导致显示/绑定不匹配（#19835 的 Copilot 第 6 轮 review）。
    // 使用现代 kitty 风格终端的用户将平台动作修饰符
    // 拼写为 ``super`` / ``win``。
    expect(parseVoiceRecordKey('meta+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('cmd+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('command+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses named keys (space, enter, tab, escape, backspace, delete)', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    // CLI 的 prompt_toolkit 的每个命名 token 来自 ``c-<name>`` 集
    // 同时接受规范名称及其常见别名。
    expect(parseVoiceRecordKey('ctrl+space')).toEqual({
      ch: 'space',
      mod: 'ctrl',
      named: 'space',
      raw: 'ctrl+space'
    })
    expect(parseVoiceRecordKey('alt+enter').named).toBe('enter')
    expect(parseVoiceRecordKey('alt+return').named).toBe('enter') // ``return`` ↔ ``enter``
    expect(parseVoiceRecordKey('ctrl+tab').named).toBe('tab')
    expect(parseVoiceRecordKey('ctrl+escape').named).toBe('escape')
    expect(parseVoiceRecordKey('ctrl+esc').named).toBe('escape') // ``esc`` 别名
    expect(parseVoiceRecordKey('ctrl+backspace').named).toBe('backspace')
    expect(parseVoiceRecordKey('ctrl+delete').named).toBe('delete')
    expect(parseVoiceRecordKey('ctrl+del').named).toBe('delete') // ``del`` 别名
  })

  it('falls back to Ctrl+B for unrecognised multi-character tokens', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // 拼写错误 / 不支持的名称（``ctrl+spcae``、``ctrl+f5`` 等）回退
    // 到文档中记录的 Ctrl+B 默认值，而不是静默禁用绑定。
    expect(parseVoiceRecordKey('ctrl+spcae')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+f5')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  // #19835 的第 3 轮 Copilot review 回归测试。
  it('does not throw on non-string YAML scalars — falls back instead', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // ``config.get full`` 返回原始 YAML 值；``voice.record_key: 1``
    // 或 ``voice.record_key: true`` 否则会崩溃 ``.trim()``。
    expect(parseVoiceRecordKey(1 as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(true as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(null as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(undefined as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey({} as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects multi-modifier chords rather than silently dropping extras', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // 之前 ``ctrl+alt+r`` 被解析为 ``ctrl+r``，``cmd+ctrl+b`` 被解析为
    // ``super+b`` —— 拼写错误会静默绑定不同的快捷键。现在
    // 多修饰符拼写回退到文档中记录的默认值。
    expect(parseVoiceRecordKey('ctrl+alt+r')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('cmd+ctrl+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+ctrl+space')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  // #19835 的第 4 轮 Copilot review 回归测试。
  it('rejects bare-char configs without an explicit modifier', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // 经典 CLI 的 prompt_toolkit 将裸字符配置绑定到键本身
    //（``c-o`` 需要显式修饰符）；将 ``o`` 重写为
    // ``ctrl+o`` 会静默使两个运行时产生分歧。拒绝。
    expect(parseVoiceRecordKey('o')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('space')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('escape')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects ctrl+c / ctrl+d / ctrl+l — reserved by the TUI input handler', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // ``useInputHandlers()`` 在 voice 检查之前拦截这些键，
    // 因此像 ``ctrl+c`` 这样的绑定会被宣传但永远不会触发。
    // 回退到文档中记录的默认值，而不是对用户撒谎。
    expect(parseVoiceRecordKey('ctrl+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    // 这些字母的 Alt 修饰符版本不会被拦截，因此
    // 仍然可用。
    expect(parseVoiceRecordKey('alt+c').mod).toBe('alt')
    // ``ctrl+x`` 是故意允许的 —— 仅在 queue-edit 期间拦截
    //（``queueEditIdx !== null``），因此 voice 绑定在
    // 大部分 session 中可用（Copilot 第 8 轮 review）。
    expect(parseVoiceRecordKey('ctrl+x').mod).toBe('ctrl')
    expect(parseVoiceRecordKey('ctrl+x').ch).toBe('x')
  })

  it('rejects super+{c,d,l,v} on macOS — action-mod chords are claimed before voice', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('darwin')

    // 在 macOS 上 super+c/d/l/v 是复制 / 退出 / 清除 / 粘贴。在
    // 解析时拒绝，这样 /voice status 不会宣传无效的绑定。
    expect(parseVoiceRecordKey('super+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+v')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    // 其他 super 字母仍然可用（没有全局快捷键占用它们）。
    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('super+o').mod).toBe('super')
  })

  it('allows super+{c,d,l,v} on Linux/Windows — those globals key off Ctrl, not Super', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    // 非 Mac 上的 Kitty/CSI-u 用户将 Cmd/Super 报告为 ``key.super``，
    // 但 TUI 的全局快捷键（复制/退出/清除/粘贴）在那里使用
    // Ctrl，因此 ``super+<letter>`` 不会冲突。拒绝会
    // 静默将有效配置强制为 Ctrl+B（Copilot 第 8 轮 review）。
    expect(parseVoiceRecordKey('super+c').mod).toBe('super')
    expect(parseVoiceRecordKey('super+d').mod).toBe('super')
    expect(parseVoiceRecordKey('super+l').mod).toBe('super')
    expect(parseVoiceRecordKey('super+v').mod).toBe('super')
  })

  it('rejects alt+{c,d,l} on macOS — meta-as-alt collides with isAction', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('darwin')

    // hermes-ink 在许多终端上将 Alt 报告为 ``key.meta``，
    // darwin 上的 ``isActionMod`` 接受 ``key.meta`` 作为动作
    // 修饰符。因此 ``alt+c`` / ``alt+d`` / ``alt+l`` 在 voice
    // 运行之前被 isCopyShortcut / isAction('d') / isAction('l') 占用
    //（#19835 的 Copilot 第 12 轮 review）。
    expect(parseVoiceRecordKey('alt+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    // darwin 上的其他 alt 字母仍然可用。
    expect(parseVoiceRecordKey('alt+r').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+space').mod).toBe('alt')
  })

  it('allows alt+{c,d,l} on Linux/Windows — non-mac isAction keys off Ctrl', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    // 在 Linux/Windows 上 ``isActionMod`` 忽略 key.meta，因此 alt+<letter>
    // 不会与复制/退出/清除冲突。这些配置保持可用。
    expect(parseVoiceRecordKey('alt+c').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+d').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+l').mod).toBe('alt')
  })

  // #19835 的第 5 轮 Copilot review 回归测试。
  it('super+<key> does NOT fire on key.meta-only events (Alt+X false-fire guard)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')

    // hermes-ink 为 Alt/Option 以及某些 macOS 终端上的裸 Esc
    // 设置 ``key.meta``。super 分支曾经接受
    // ``isMac && key.meta`` 作为 Cmd 回退，这使得 super+<key>
    // 绑定在 Alt+<key> / 裸 Esc 上静默触发。
    const superB = parseVoiceRecordKey('super+b')
    const superSpace = parseVoiceRecordKey('super+space')
    const superEscape = parseVoiceRecordKey('super+escape')

    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, ' ', superSpace)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', superEscape)).toBe(false)
  })

  // #19835 的第 6 轮 Copilot review 回归测试。
  it('default ctrl+b does NOT fire on Alt+B via isActionMod meta leak', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, isVoiceToggleKey } = await importPlatform('darwin')

    // darwin 上的 ``isActionMod(key)`` 曾经接受 ``key.meta`` 作为
    // 动作修饰符，因此 Alt+B（key.meta=true）触发了默认的
    // ctrl+b 绑定。现在 Cmd 回退路径在 macOS 上需要字面的
    // ``key.super`` 并拒绝 ``key.meta``。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(false)
    // darwin 上的字面 Ctrl+B 和 Cmd+B（kitty 风格）仍然可用。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
  })

  it('ctrl+<key> rejects chords with extra alt / meta / super bits', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    // ``ctrl+o`` 必须仅在字面 Ctrl+O 时触发，而不是在
    // Ctrl+Alt+O / Ctrl+Cmd+O / Ctrl+Meta+O 时触发 —— 否则运行时
    // 匹配的快捷键与解析器允许配置的
    // 不同。
    expect(isVoiceToggleKey({ alt: true, ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: true, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'o', ctrlO)).toBe(false)
    // 验证：普通的 Ctrl+O 仍然触发。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
  })

  it('super+<key> rejects chords with extra ctrl / alt / meta bits', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const superB = parseVoiceRecordKey('super+b')

    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: true }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: true }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'b', superB)).toBe(false)
    // 验证：普通的 Super+B 仍然触发。
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', superB)).toBe(true)
  })

  it('alt+escape does not fire on bare Esc meta-shape', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const altEscape = parseVoiceRecordKey('alt+escape')

    // 某些终端将裸 Esc 显示为 meta=true + escape=true。
    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', altEscape)).toBe(false)
    // 显式 alt 位（kitty 风格）仍然触发配置的快捷键。
    expect(isVoiceToggleKey({ alt: true, ctrl: false, escape: true, meta: false, super: false }, '', altEscape)).toBe(true)
  })

  it('rejects matches when Shift is held (different chord than configured)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')

    // 解析器拒绝多修饰符配置如 ``ctrl+shift+tab``，
    // 因此运行时匹配器也必须拒绝按住 Shift 的事件 ——
    // 否则 ``ctrl+tab`` 会在 Ctrl+Shift+Tab 时触发。
    const ctrlTab = parseVoiceRecordKey('ctrl+tab')
    const altEnter = parseVoiceRecordKey('alt+enter')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: true, super: false, tab: true }, '', ctrlTab)).toBe(false)
    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, return: true, shift: true, super: false }, '', altEnter)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: true, super: false }, 'o', ctrlO)).toBe(false)

    // Sanity: same events without Shift still fire.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: false, super: false, tab: true }, '', ctrlTab)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: false, super: false }, 'o', ctrlO)).toBe(true)
  })
})

describe('formatVoiceRecordKey (#18994)', () => {
  it('renders as the user expects in /voice status', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+b'))).toBe('Ctrl+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+o'))).toBe('Ctrl+O')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+r'))).toBe('Alt+R')
    // ``super``/``win`` 在非 Mac 上渲染为 ``Super``，这样提示
    // 不会告诉 Linux/Windows 用户按他们没有的 Cmd 键。
    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Super+B')
  })

  it('renders named keys in title case (Ctrl+Space, Ctrl+Enter)', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+space'))).toBe('Ctrl+Space')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+enter'))).toBe('Alt+Enter')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+esc'))).toBe('Ctrl+Escape')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+space'))).toBe('Super+Space')
  })
})

describe('isVoiceToggleKey honours configured record key (#18994)', () => {
  it('binds the configured letter, not hardcoded b', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
    // 旧的硬编码 'b' 在用户配置了 'o' 时不能匹配。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlO)).toBe(false)
  })

  it('alt+<letter> binding matches alt OR meta (terminal-protocol parity)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const altR = parseVoiceRecordKey('alt+r')

    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'r', altR)).toBe(false)
  })

  it('binds named keys via ink event flags (space → ch === " ", enter → key.return, …)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')

    const ctrlSpace = parseVoiceRecordKey('ctrl+space')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)
    // 单字符 ``b`` 不能匹配 ``space`` 配置的绑定。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlSpace)).toBe(false)
    // 没有配置修饰符的 Space 也不能触发。
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, ' ', ctrlSpace)).toBe(false)

    const ctrlEnter = parseVoiceRecordKey('ctrl+enter')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: true, super: false }, '', ctrlEnter)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: false, super: false }, '', ctrlEnter)).toBe(false)

    const altTab = parseVoiceRecordKey('alt+tab')
    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(true)
    expect(isVoiceToggleKey({ alt: false, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(false)

    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')
    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, escape: false, meta: false, super: false }, '', ctrlEscape)).toBe(false)

    const ctrlBackspace = parseVoiceRecordKey('ctrl+backspace')
    expect(isVoiceToggleKey({ backspace: true, ctrl: true, meta: false, super: false }, '', ctrlBackspace)).toBe(true)

    const ctrlDelete = parseVoiceRecordKey('ctrl+delete')
    expect(isVoiceToggleKey({ ctrl: true, delete: true, meta: false, super: false }, '', ctrlDelete)).toBe(true)
  })

  it('omitted configured key falls back to ctrl+b (back-compat)', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')

    // 没有第三个参数 → DEFAULT_VOICE_RECORD_KEY → Ctrl+B 行为。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o')).toBe(false)
  })

  // #19835 的 Copilot review 回归测试：之前的实现
  // 在每个配置键的 ``ctrl`` 分支中接受了 ``isActionMod(key)``，
  // 因此裸 Esc（hermes-ink 在某些 macOS 终端上报告为
  // ``key.meta``）触发了 ``ctrl+escape``，
  // Alt+Space / Alt+Tab 触发了 ``ctrl+space`` / ``ctrl+tab``。
  // 回退现在仅限于文档中记录的默认值（``ctrl+b``）。
  it('ctrl+escape does NOT fire on bare Esc via key.meta on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')

    // 旧式 macOS 终端上的裸 Esc：``key.meta: true``、``key.escape: true``，没有 ctrl。
    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', ctrlEscape)).toBe(false)
    // 真正的 Ctrl+Esc 仍然触发。
    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
  })

  it('ctrl+space does NOT fire on Alt+Space on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlSpace = parseVoiceRecordKey('ctrl+space')

    // Alt+Space 显示为 ``key.meta: true`` 加上 space 字符。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, ' ', ctrlSpace)).toBe(false)
    // 真正的 Ctrl+Space 仍然触发。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)
  })

  it('default ctrl+b accepts raw Ctrl+B and kitty-style Cmd+B on macOS', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, isVoiceToggleKey } = await importPlatform('darwin')

    // 原始 Ctrl+B：始终可用。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    // 通过 kitty 风格的 ``key.super`` 的 Cmd+B：仍然可用。
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    // 通过旧式 ``key.meta`` 的 Cmd+B 不再可用 —— ``key.meta`` 是
    // hermes-ink 的 Alt 信号，因此接受它会将 Alt+B 泄漏到
    // 默认绑定中（#19835 的 Copilot 第 6 轮 review）。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(false)
  })

  it('custom ctrl+<letter> does NOT accept Cmd fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    // 只有 ``ctrl+b`` 获得动作修饰符回退；``ctrl+o`` 必须是
    // 字面的 Ctrl 位 —— 否则 Cmd+O 会抢占快捷键。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
  })

  it('super+b renders "Cmd+B" on darwin and requires the literal key.super bit', async () => {
    const { formatVoiceRecordKey, isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const superB = parseVoiceRecordKey('super+b')

    expect(formatVoiceRecordKey(superB)).toBe('Cmd+B')
    // Kitty 风格：key.super 触发绑定。
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', superB)).toBe(true)
    // ``key.meta`` 不被接受 —— hermes-ink 对 Alt 也使用 meta，
    // 因此在这里接受它会使 super+b 在 Alt+B 时静默触发
    //（#19835 的 Copilot 第 5 轮 review）。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', superB)).toBe(false)
    // 同时按住 Ctrl → 拒绝（不同的快捷键）。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'b', superB)).toBe(false)
  })

  // #19835 的第 2 轮 Copilot review 回归测试。
  it('super+b renders "Super+B" on Linux (not "Cmd+B")', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Super+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Super+B')
  })

  it('super+b still renders "Cmd+B" on macOS', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('darwin')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Cmd+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Cmd+B')
  })

  it('ctrl+b aliases (control+b, "ctrl + b") still accept Cmd+B fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const controlB = parseVoiceRecordKey('control+b')
    const spacedB = parseVoiceRecordKey('ctrl + b')

    // 两者在语义上都解析为文档中记录的默认值；两者都必须
    // 保留 macOS 上通过 kitty 风格的 key.super 实现的 Cmd+B 肌肉记忆回退。
    // ``key.meta`` 不被接受 —— 那是 hermes-ink 的 Alt 信号
    //（第 6 轮 review），因此旧式终端用户使用严格的 Ctrl+B。
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', controlB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', spacedB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', controlB)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', spacedB)).toBe(true)
    // 字面 Ctrl+B 仍然触发。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', controlB)).toBe(true)
    // 并且仍然拒绝不同字母的 ctrl 位。
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', controlB)).toBe(false)
  })
})

describe('isMacActionFallback', () => {
  it('routes raw Ctrl+K and Ctrl+W to readline kill-to-end / delete-word on macOS', async () => {
    const { isMacActionFallback } = await importPlatform('darwin')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(true)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(true)
    // 当 Cmd（meta/super）被按住时不能触发 —— 那些是不同的快捷键。
    expect(isMacActionFallback({ ctrl: true, meta: true, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: true }, 'w', 'w')).toBe(false)
  })

  it('is a no-op on non-macOS (Linux routes Ctrl+K/W through isActionMod directly)', async () => {
    const { isMacActionFallback } = await importPlatform('linux')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(false)
  })
})
