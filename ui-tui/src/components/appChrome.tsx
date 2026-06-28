import { Box, type ScrollBoxHandle, stringWidth, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { type ReactNode, type RefObject, useEffect, useMemo, useRef, useState } from 'react'
import unicodeSpinners from 'unicode-animations'

import { $delegationState } from '../app/delegationStore.js'
import type { IndicatorStyle, Notice } from '../app/interfaces.js'
import { useTurnSelector } from '../app/turnStore.js'
import { DEV_CREDITS_MODE } from '../config/env.js'
import { FACES } from '../content/faces.js'
import { VERBS } from '../content/verbs.js'
import { fmtDuration } from '../domain/messages.js'
import { stickyPromptFromViewport } from '../domain/viewport.js'
import { buildSubagentTree, treeTotals, widthByDepth } from '../lib/subagentTree.js'
import { fmtK } from '../lib/text.js'
import { useScrollbarSnapshot, useViewportSnapshot } from '../lib/viewportStore.js'
import type { Theme } from '../theme.js'
import type { Msg, Usage } from '../types.js'

const FACE_TICK_MS = 2500
const HEART_COLORS = ['#ff5fa2', '#ff4d6d']

// 保持 verb 段宽度稳定，这样状态栏右侧内容在 ticker
// 在短/长动词之间切换时不会抖动。
export const VERB_PAD_LEN = VERBS.reduce((max, v) => Math.max(max, v.length), 0) + 1 // + 省略号
export const padVerb = (verb: string) => `${verb}…`.padEnd(VERB_PAD_LEN, ' ')

// `emoji` 和 `ascii` 指示器样式的紧凑替代方案。
// 每个条目是固定宽度（显示宽度）的字形。
const EMOJI_FRAMES = ['⚕ ', '🌀', '🤔', '✨', '🍵', '🔮']
const ASCII_FRAMES = ['|', '/', '-', '\\']

// 对 spinner 风格指示器使用更快的 tick — 它们只有在
// 接近其原始间隔的帧率下才看起来像运动。
const SPINNER_TICK_MS = 100

interface IndicatorRender {
  frame: string
  intervalMs: number
  // 当为 false 时，FaceTicker 隐藏旋转的 verb，只显示
  // 字形 + 持续时间。让 `unicode` 保持最小化，而其他样式
  // 保留用户与 running… 状态关联的 verb 轮换风格。
  showVerb: boolean
}

const renderIndicator = (style: IndicatorStyle, tick: number): IndicatorRender => {
  if (style === 'kaomoji') {
    return { frame: FACES[tick % FACES.length] ?? '', intervalMs: FACE_TICK_MS, showVerb: true }
  }

  if (style === 'emoji') {
    return {
      frame: EMOJI_FRAMES[tick % EMOJI_FRAMES.length] ?? '⚕ ',
      intervalMs: SPINNER_TICK_MS * 6,
      showVerb: true
    }
  }

  if (style === 'ascii') {
    return {
      frame: ASCII_FRAMES[tick % ASCII_FRAMES.length] ?? '|',
      intervalMs: SPINNER_TICK_MS,
      showVerb: true
    }
  }

  // 'unicode' — 盲文 spinner（固定 1 列）。原始间隔约
  // 80ms；遵循它但限制在安全最小值以下，以保持
  // React 重新渲染合理。此样式面向想要最干净状态
  // 的用户，因此也没有 verb 轮换。
  const spinner = unicodeSpinners.braille
  const frame = spinner.frames[tick % spinner.frames.length] ?? '⠋'

  return { frame, intervalMs: Math.max(SPINNER_TICK_MS, spinner.interval), showVerb: false }
}

// `FACES` / `EMOJI_FRAMES` 是静态的，因此在模块加载时测量一次
// 最宽字形，而不是在每次状态渲染时重新扫描。
const KAOMOJI_FRAME_WIDTH = FACES.reduce((max, f) => Math.max(max, stringWidth(f)), 1)
const EMOJI_FRAME_WIDTH = EMOJI_FRAMES.reduce((max, f) => Math.max(max, stringWidth(f)), 1)

const indicatorFrameWidth = (style: IndicatorStyle): number => {
  if (style === 'kaomoji') {
    return KAOMOJI_FRAME_WIDTH
  }

  if (style === 'emoji') {
    return EMOJI_FRAME_WIDTH
  }

  // 'ascii' 和 'unicode' 是单列字形。
  return 1
}

// 由 `fmtDuration` 本身推导的已过时间时钟的有界宽度，
// 这样预留/预算与实际渲染的内容保持一致（它在单位之间
// 发出空格，例如 `59m 59s` / `99h 59m`）。超过此范围
// （100h+）的持续时间将直接裁剪，而不是预留无限宽度。
export const MAX_DURATION_WIDTH = Math.max(
  stringWidth(fmtDuration(59 * 60_000 + 59_000)), // "59m 59s"
  stringWidth(fmtDuration(99 * 3_600_000 + 59 * 60_000)) // "99h 59m"
)

// 为忙碌指示器预留的显示宽度，使其 verb + 已过时间尾部
// 不会在窄终端上把 model 挤出屏幕。样式感知：
// `unicode` 是裸 1 列盲文 spinner，无 verb；而 kaomoji/emoji/
// ascii 添加固定宽度 verb；任何样式都添加有界的已过时间尾部。
// 镜像 FaceTicker 的 `frame + verbSegment + durationSegment` 布局。
export const busyIndicatorWidth = (style: IndicatorStyle, hasDuration: boolean): number => {
  const { showVerb } = renderIndicator(style, 0)
  const verb = showVerb ? 1 + VERB_PAD_LEN : 0
  // ` · ` 加上有界的时钟（例如 `59m 59s`）。
  const duration = hasDuration ? stringWidth(' · ') + MAX_DURATION_WIDTH : 0

  return indicatorFrameWidth(style) + verb + duration
}

function FaceTicker({ color, startedAt, style }: { color: string; startedAt?: null | number; style: IndicatorStyle }) {
  const [tick, setTick] = useState(() => Math.floor(Math.random() * 1000))
  const [verbTick, setVerbTick] = useState(() => Math.floor(Math.random() * VERBS.length))
  const [now, setNow] = useState(() => Date.now())

  // 预计算活跃样式的节奏 + verb 可见性，这样 `/indicator`
  // 切换时重新设置间隔（对于无 verb 样式如 `unicode` 跳过
  // verb 计时器），而不会留下之前的计时器悬挂。
  const { intervalMs, showVerb } = renderIndicator(style, 0)

  useEffect(() => {
    const glyph = setInterval(() => setTick(n => n + 1), intervalMs)
    const clock = setInterval(() => setNow(Date.now()), 1000)
    // Verb 计时器受 `showVerb` 门控 — `unicode` 样式完全隐藏 verb，
    // 因此循环 `verbTick` 将是不必要的重新渲染。
    const verb = showVerb ? setInterval(() => setVerbTick(n => n + 1), FACE_TICK_MS) : null

    return () => {
      clearInterval(glyph)
      clearInterval(clock)

      if (verb !== null) {
        clearInterval(verb)
      }
    }
  }, [intervalMs, showVerb])

  const { frame } = renderIndicator(style, tick)
  const verb = VERBS[verbTick % VERBS.length] ?? ''
  const verbSegment = showVerb ? ` ${padVerb(verb)}` : ''
  // 前导空格在 verb 段隐藏时在 frame 和 duration 之间保持间距
  // （例如 `unicode` spinner 样式）。当 verb 显示时，其尾部
  // 填充已经提供了间距，所以额外的空格无害。
  const durationSegment = startedAt ? ` · ${fmtDuration(now - startedAt)}` : ''

  return (
    <Text color={color}>
      {frame}
      {verbSegment}
      {durationSegment}
    </Text>
  )
}

function ctxBarColor(pct: number | undefined, t: Theme) {
  if (pct == null) {
    return t.color.muted
  }

  if (pct >= 95) {
    return t.color.statusCritical
  }

  if (pct > 80) {
    return t.color.statusBad
  }

  if (pct >= 50) {
    return t.color.statusWarn
  }

  return t.color.statusGood
}

function statusSessionCountLabel(count: number) {
  return `${count} ${count === 1 ? 'session' : 'sessions'}`
}

// 根据其级别为 credits 通知着色。通知文本已携带其自身的
// 字形（⚠ • ✕ ✓）来自 Python 策略 — 我们只在这里着色，
// 绝不添加额外字形。`success` 映射到主题的绿色状态颜色。
function noticeColor(level: Notice['level'], t: Theme): string {
  if (level === 'error') {
    return t.color.error
  }

  if (level === 'warn') {
    return t.color.warn
  }

  if (level === 'success') {
    return t.color.statusGood
  }

  // 'info' / 未定义 — 保持可读但低调。
  return t.color.accent
}

function ctxBar(pct: number | undefined, w = 10) {
  const p = Math.max(0, Math.min(100, pct ?? 0))
  const filled = Math.round((p / 100) * w)

  return '█'.repeat(filled) + '░'.repeat(w - filled)
}

// `minLeftContent` 是高优先级左侧段的显示宽度
// （状态指示器 + model + context）。预留它使得右侧的
// cwd/branch 段在窄终端上首先让出空间，而不是将
// 加载指示器和 model 压缩到没有空间。
export function statusRuleWidths(cols: number, cwdLabel: string, minLeftContent = 0) {
  const width = Math.max(1, Math.floor(cols || 1))
  const desiredSeparatorWidth = width >= 24 ? 3 : 1
  const baseMinLeft = width >= 24 ? 8 : 1
  // 预留不超过终端宽度；不低于历史下限。
  // 默认 `minLeftContent = 0` 时与旧行为完全相同，
  // 因此不传入内容的调用方不受影响。
  const minLeftWidth = Math.min(width, Math.max(baseMinLeft, Math.floor(minLeftContent)))
  const maxRightWidth = Math.max(0, width - desiredSeparatorWidth - minLeftWidth)

  if (!cwdLabel || maxRightWidth <= 0) {
    return { leftWidth: width, rightWidth: 0, separatorWidth: 0 }
  }

  const rightWidth = Math.max(0, Math.min(stringWidth(cwdLabel), maxRightWidth))
  const separatorWidth = rightWidth > 0 ? desiredSeparatorWidth : 0
  const leftWidth = Math.max(1, width - separatorWidth - rightWidth)

  return { leftWidth, rightWidth, separatorWidth }
}

// 状态栏规则中较低优先级尾部段的渐进式显示。
// 随着终端变窄，我们首先移除最不重要的部分
// （cost → bg → voice → compressions → duration → context bar），
// 在 bar 断点以下，上下文读出折叠为纯 token 计数。
// Status 和 model 从不在此处限制 — 它们由
// `statusRuleWidths` 保证有空间。
export interface StatusBarSegments {
  bar: boolean
  bg: boolean
  compactCtx: boolean
  compressions: boolean
  cost: boolean
  duration: boolean
  subagents: boolean
  voice: boolean
}

export function statusBarSegments(cols: number): StatusBarSegments {
  const w = Math.max(1, Math.floor(cols || 1))

  return {
    compactCtx: w < 72,
    bar: w >= 72,
    duration: w >= 76,
    compressions: w >= 80,
    voice: w >= 84,
    bg: w >= 88,
    subagents: w >= 92,
    cost: w >= 96
  }
}

function SpawnHud({ t }: { t: Theme }) {
  // 仅在 session 实际扇出时出现的紧凑 HUD。
  // 当深度或并发接近上限时，颜色升级为 warn/error。
  const delegation = useStore($delegationState)
  const subagents = useTurnSelector(state => state.subagents)

  const tree = useMemo(() => buildSubagentTree(subagents), [subagents])
  const totals = useMemo(() => treeTotals(tree), [tree])

  if (!totals.descendantCount && !delegation.paused) {
    return null
  }

  const maxDepth = delegation.maxSpawnDepth
  const maxConc = delegation.maxConcurrentChildren
  const depth = Math.max(0, totals.maxDepthFromHere)
  const active = totals.activeCount

  // `max_concurrent_children` 是每父级上限，而非全局上限。
  // `activeCount` 汇总树中所有运行中的 agent，对于
  // 多编排器运行会过度警告。树最宽的层级更接近
  // "可能命中单个父级槽位预算的最大并发 spawn 数"。
  const widestLevel = widthByDepth(tree).reduce((a, b) => Math.max(a, b), 0)
  const depthRatio = maxDepth ? depth / maxDepth : 0
  const concRatio = maxConc ? widestLevel / maxConc : 0
  const ratio = Math.max(depthRatio, concRatio)

  const color = delegation.paused || ratio >= 1 ? t.color.error : ratio >= 0.66 ? t.color.warn : t.color.muted

  const pieces: string[] = []

  if (delegation.paused) {
    pieces.push('⏸ paused')
  }

  if (totals.descendantCount > 0) {
    const depthLabel = maxDepth ? `${depth}/${maxDepth}` : `${depth}`
    pieces.push(`d${depthLabel}`)

    if (active > 0) {
      // 标签将最宽层级计数（驱动上面的 concRatio）与
      // 总活跃计数配对以提供上下文。`W/cap` 触发警告，
      // `+N` 是树中当前运行的其他所有内容。
      const extra = Math.max(0, active - widestLevel)
      const widthLabel = maxConc ? `${widestLevel}/${maxConc}` : `${widestLevel}`
      const suffix = extra > 0 ? `+${extra}` : ''
      pieces.push(`⚡${widthLabel}${suffix}`)
    }
  }

  const atCap = depthRatio >= 1 || concRatio >= 1

  return (
    <Text color={color}>
      {atCap ? ' │ ⚠ ' : ' │ '}
      {pieces.join(' ')}
    </Text>
  )
}

function SessionDuration({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [startedAt])

  return fmtDuration(now - startedAt)
}

function IdleSince({ endedAt }: { endedAt: number }) {
  // 自上次最终 agent 响应以来的时间。每秒重新 tick，
  // 与 SessionDuration 一样，使读数在 session 空闲时保持实时更新。
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [endedAt])

  return `✓ ${fmtDuration(now - endedAt)}`
}

const effortLabel = (effort?: string) => {
  const value = String(effort ?? '')
    .trim()
    .toLowerCase()

  return value && value !== 'medium' && value !== 'normal' && value !== 'default' ? value : ''
}

const shortModelLabel = (model: string) =>
  model
    .split('/')
    .pop()!
    .replace(/^claude[-_]/, '')
    .replace(/^anthropic[-_]/, '')
    .replace(/[-_]/g, ' ')
    .replace(/\b(\d+)\s+(\d+)\b/g, '$1.$2')
    .trim()

const modelLabel = (model: string, effort?: string, fast?: boolean) =>
  [shortModelLabel(model), effortLabel(effort), fast ? 'fast' : ''].filter(Boolean).join(' ')

export function GoodVibesHeart({ tick, t }: { tick: number; t: Theme }) {
  const [active, setActive] = useState(false)
  const [color, setColor] = useState(t.color.accent)

  useEffect(() => {
    if (tick <= 0) {
      return
    }

    const palette = [t.color.error, t.color.warn, t.color.accent]
    setColor(palette[Math.floor(Math.random() * palette.length)]!)
    setActive(true)

    const id = setTimeout(() => setActive(false), 650)

    return () => clearTimeout(id)
  }, [t.color.accent, tick])

  if (!active) {
    return null
  }

  return <Text color={color}>♥</Text>
}

export function StatusRule({
  cwdLabel,
  cols,
  busy,
  status,
  statusColor,
  model,
  modelFast,
  modelReasoningEffort,
  indicatorStyle = 'kaomoji',
  notice,
  usage,
  bgCount,
  lastTurnEndedAt,
  liveSessionCount,
  sessionStartedAt,
  showCost,
  turnStartedAt,
  voiceLabel,
  onSessionCountClick,
  t
}: StatusRuleProps) {
  const pct = usage.context_percent
  const barColor = ctxBarColor(pct, t)
  const segs = statusBarSegments(cols)

  // 在窄终端上，上下文读出折叠为纯 token 计数
  // （`12k tok`），可视填充条完全省略。
  const ctxLabel = usage.context_max
    ? segs.compactCtx
      ? `${fmtK(usage.context_used ?? 0)} tok`
      : `${fmtK(usage.context_used ?? 0)}/${fmtK(usage.context_max)}`
    : usage.total > 0
      ? `${fmtK(usage.total)} tok`
      : ''

  const bar = !segs.compactCtx && usage.context_max ? ctxBar(pct) : ''
  const modelText = modelLabel(model, modelReasoningEffort, modelFast)

  // Credits 通知替换 status/verb 槽，但仅在空闲时 —
  // 忙碌时 FaceTicker 始终获胜（R1 渲染优先级）。
  // 通知文本携带其自身字形；我们只着色（R1）并让其收缩（R3-M7）。
  const showNotice = !busy && !!notice?.text
  // 通知槽是可收缩的（flexShrink={1}，truncate-end），
  // 因此在 essentials 预算中仅为其预留较小的有界宽度 —
  // 足以让短通知不被压碎，但长通知会省略号截断，
  // 而不是将 `model │ ctx` 挤出屏幕（R3-M7）。
  // 上限为通知自身宽度，因此短通知精确预留所需空间。
  const NOTICE_RESERVE_MAX = 24
  const noticeReserve = showNotice ? Math.min(stringWidth(notice!.text), NOTICE_RESERVE_MAX) : 0

  // 必须保留的左侧段的宽度（indicator + model + context）。
  // 它们固定（永不收缩）并预留空间，使右侧的 cwd/branch 首先让出。
  // 忙碌 face 宽度取决于当前 /indicator 样式
  // （kaomoji 宽 + verb；unicode 是裸 1 列 spinner）。
  // 当通知占据该槽时仅预留 `noticeReserve`（它会收缩/截断）。
  const slotWidth = busy
    ? busyIndicatorWidth(indicatorStyle, turnStartedAt != null)
    : showNotice
      ? noticeReserve
      : stringWidth(status)

  const essentialWidth =
    stringWidth('─ ') +
    slotWidth +
    stringWidth(' │ ') +
    stringWidth(modelText) +
    (ctxLabel ? stringWidth(' │ ') + stringWidth(ctxLabel) : 0)

  const { leftWidth, rightWidth, separatorWidth } = statusRuleWidths(cols, cwdLabel, essentialWidth)

  // 尾部整体段的渐进式显示：段仅在适合固定 essentials
  // 之后剩余空间时渲染，按降优先级顺序评估 —
  // bar、duration、compressions、voice、session count、bg、cost。
  // 较低优先级段首先丢弃，没有任何段中间截断，
  // 因此 status/model/context 永远不会被压缩。
  const SEP = stringWidth(' │ ')
  let tailBudget = Math.max(0, leftWidth - essentialWidth)
  const fits = (w: number) => {
    if (tailBudget >= w) {
      tailBudget -= w

      return true
    }

    return false
  }

  const sessionCountText = liveSessionCount > 0 ? statusSessionCountLabel(liveSessionCount) : ''
  const compressions = typeof usage.compressions === 'number' ? usage.compressions : 0
  const costText = typeof usage.cost_usd === 'number' ? `$${usage.cost_usd.toFixed(4)}` : ''
  // 仅开发模式读数（HERMES_DEV_CREDITS）。服务器在该标志未开启时
  // 完全省略此键，因此此段对普通用户自动隐藏。micros→cents
  // 是合法金额格式（显示格式化）— 绝不 parseFloat *_usd。
  // 有符号：session 中途充值若增加余额则产生负 Δ（诚实）。
  const devCreditsText =
    typeof usage.dev_credits_spent_micros === 'number'
      ? `Δ ${(usage.dev_credits_spent_micros / 10000).toFixed(1)}¢`
      : ''

  const showBar = !!bar && fits(SEP + stringWidth(`[${bar}] ${pct != null ? `${pct}%` : ''}`))
  const showDuration = segs.duration && !!sessionStartedAt && fits(SEP + MAX_DURATION_WIDTH)
  // 空闲时钟 — 自上次最终 agent 响应以来的时间。
  // 忙碌时隐藏（FaceTicker 的已过时间尾部覆盖活跃回合）
  // 以及首个回合完成前。共享 duration 断点和宽度预留。
  const showIdle = segs.duration && !busy && lastTurnEndedAt != null && fits(SEP + stringWidth('✓ ') + MAX_DURATION_WIDTH)
  const showCompressions = segs.compressions && compressions > 0 && fits(SEP + stringWidth(`cmp ${compressions}`))
  const showVoice = segs.voice && !!voiceLabel && fits(SEP + stringWidth(voiceLabel))
  const showSessionCount = !!sessionCountText && fits(SEP + stringWidth(sessionCountText))
  const showBg = segs.bg && bgCount > 0 && fits(SEP + stringWidth(`${bgCount} bg`))
  const subagentCount = typeof usage.active_subagents === 'number' ? usage.active_subagents : 0
  const showSubagents = segs.subagents && subagentCount > 0 && fits(SEP + stringWidth(`⛓ ${subagentCount}`))
  const showCostSeg = segs.cost && showCost && !!costText && fits(SEP + stringWidth(costText))
  // 无 segs 标志 / 无 showCost 耦合 — 它是服务器门控的开发读数，
  // 最低优先级，因此在窄终端上最后消耗尾部预算并最先丢弃。
  const showDevCredits = !!devCreditsText && fits(SEP + stringWidth(devCreditsText))

  const handleSessionCountClick = (event: { stopImmediatePropagation?: () => void }) => {
    event.stopImmediatePropagation?.()
    onSessionCountClick?.()
  }

  const sessionCountNode = onSessionCountClick ? (
    <Box flexShrink={0} onClick={handleSessionCountClick}>
      <Text color={t.color.accent}> │ {sessionCountText}</Text>
    </Box>
  ) : (
    <Text color={t.color.muted}> │ {sessionCountText}</Text>
  )

  return (
    <Box height={1}>
      <Box flexDirection="row" flexShrink={1} overflow="hidden" width={leftWidth}>
        {/* 前导固定 chrome：边框 + 忙碌 face / 空闲状态。
            当通知占据该槽时，status 文本被移除 —
            通知作为单独的可收缩框在下方渲染，
            使长通知省略号截断而不是压缩 model │ ctx（R3-M7）。 */}
        <Box flexDirection="row" flexShrink={0}>
          <Text color={t.color.border}>{'─ '}</Text>
          {busy ? (
            <FaceTicker color={statusColor} startedAt={turnStartedAt} style={indicatorStyle} />
          ) : showNotice ? null : (
            <Text color={statusColor} wrap="truncate-end">
              {status}
            </Text>
          )}
        </Box>
        {/* 通知槽 — 唯一可收缩的左侧元素（R3-M7）。位于
            flexShrink={1} 框中，使用 truncate-end，使其在
            固定的 model │ ctx 框裁剪之前先让出/省略号截断。 */}
        {showNotice ? (
          <Box flexDirection="row" flexShrink={1} overflow="hidden">
            <Text color={noticeColor(notice!.level, t)} wrap="truncate-end">
              {notice!.text}
            </Text>
          </Box>
        ) : null}
        {/* 固定 essentials — model + context 永不收缩，始终可见。 */}
        <Box flexDirection="row" flexShrink={0}>
          {DEV_CREDITS_MODE ? (
            <Text color={t.color.warn} wrap="truncate-end">
              {' (dev credits)'}
            </Text>
          ) : null}
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            {modelText}
          </Text>
          {ctxLabel ? (
            <Text color={t.color.muted} wrap="truncate-end">
              {' │ '}
              {ctxLabel}
            </Text>
          ) : null}
        </Box>
        {showBar ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            <Text color={barColor}>[{bar}]</Text> <Text color={barColor}>{pct != null ? `${pct}%` : ''}</Text>
          </Text>
        ) : null}
        {showDuration ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            <SessionDuration startedAt={sessionStartedAt!} />
          </Text>
        ) : null}
        {showIdle ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            <IdleSince endedAt={lastTurnEndedAt!} />
          </Text>
        ) : null}
        {showCompressions ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            <Text color={compressions >= 10 ? t.color.error : compressions >= 5 ? t.color.warn : t.color.muted}>
              cmp {compressions}
            </Text>
          </Text>
        ) : null}
        {showVoice ? (
          <Text
            color={
              voiceLabel!.startsWith('●') ? t.color.error : voiceLabel!.startsWith('◉') ? t.color.warn : t.color.muted
            }
            wrap="truncate-end"
          >
            {' │ '}
            {voiceLabel}
          </Text>
        ) : null}
        {showSessionCount ? sessionCountNode : null}
        {showBg ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            {bgCount} bg
          </Text>
        ) : null}
        {showSubagents ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            ⛓ {subagentCount}
          </Text>
        ) : null}
        {showCostSeg ? (
          <Text color={t.color.muted} wrap="truncate-end">
            {' │ '}
            {costText}
          </Text>
        ) : null}
        {showDevCredits ? (
          <Text color={t.color.accent} wrap="truncate-end">
            {' │ '}
            {devCreditsText}
          </Text>
        ) : null}
        {/* SpawnHud 不属于尾部预算（其宽度是动态的），因此最后
            渲染 — 任何溢出截断 HUD 本身而不是其前面的预算段。
            没有 delegation 运行时自动隐藏。 */}
        <SpawnHud t={t} />
      </Box>

      {rightWidth > 0 ? (
        <>
          <Text color={t.color.border}>{separatorWidth >= 3 ? ' ─ ' : ' '}</Text>
          <Box flexShrink={0} width={rightWidth}>
            <Text color={t.color.label} wrap="truncate-end">
              {cwdLabel}
            </Text>
          </Box>
        </>
      ) : null}
    </Box>
  )
}

export function FloatBox({ children, color }: { children: ReactNode; color: string }) {
  return (
    <Box
      alignSelf="flex-start"
      borderColor={color}
      borderStyle="double"
      flexDirection="column"
      marginTop={1}
      opaque
      paddingX={1}
    >
      {children}
    </Box>
  )
}

export function StickyPromptTracker({ messages, offsets, scrollRef, onChange }: StickyPromptTrackerProps) {
  const { atBottom, bottom, top } = useViewportSnapshot(scrollRef)
  const text = stickyPromptFromViewport(messages, offsets, top, bottom, atBottom)

  useEffect(() => onChange(text), [onChange, text])

  return null
}

export function TranscriptScrollbar({ scrollRef, t }: TranscriptScrollbarProps) {
  const [hover, setHover] = useState(false)
  const [grab, setGrab] = useState<number | null>(null)
  const grabRef = useRef<number | null>(null)
  const { scrollHeight: total, top: pos, viewportHeight: vp } = useScrollbarSnapshot(scrollRef)

  if (!vp) {
    return <Box width={1} />
  }

  const s = scrollRef.current
  const scrollable = total > vp
  const thumb = scrollable ? Math.max(1, Math.round((vp * vp) / total)) : vp
  const travel = Math.max(1, vp - thumb)
  const thumbTop = scrollable ? Math.round((pos / Math.max(1, total - vp)) * travel) : 0
  const thumbColor = grab !== null ? t.color.primary : hover ? t.color.accent : t.color.border
  const trackColor = hover ? t.color.border : t.color.muted

  const jump = (row: number, offset: number) => {
    if (!s || !scrollable) {
      return
    }

    s.scrollTo(Math.round((Math.max(0, Math.min(travel, row - offset)) / travel) * Math.max(0, total - vp)))
  }

  return (
    <Box
      flexDirection="column"
      onMouseDown={(e: { localRow?: number }) => {
        const row = Math.max(0, Math.min(vp - 1, e.localRow ?? 0))
        const off = row >= thumbTop && row < thumbTop + thumb ? row - thumbTop : Math.floor(thumb / 2)

        grabRef.current = off
        setGrab(off)
        jump(row, off)
      }}
      onMouseDrag={(e: { localRow?: number }) =>
        jump(Math.max(0, Math.min(vp - 1, e.localRow ?? 0)), grabRef.current ?? Math.floor(thumb / 2))
      }
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onMouseUp={() => {
        grabRef.current = null
        setGrab(null)
      }}
      width={1}
    >
      {!scrollable ? (
        <Text color={trackColor} dim>
          {' \n'.repeat(Math.max(0, vp - 1))}{' '}
        </Text>
      ) : (
        <>
          {thumbTop > 0 ? (
            <Text color={trackColor} dim={!hover}>
              {`${'│\n'.repeat(Math.max(0, thumbTop - 1))}${thumbTop > 0 ? '│' : ''}`}
            </Text>
          ) : null}
          {thumb > 0 ? (
            <Text color={thumbColor}>{`${'┃\n'.repeat(Math.max(0, thumb - 1))}${thumb > 0 ? '┃' : ''}`}</Text>
          ) : null}
          {vp - thumbTop - thumb > 0 ? (
            <Text color={trackColor} dim={!hover}>
              {`${'│\n'.repeat(Math.max(0, vp - thumbTop - thumb - 1))}${vp - thumbTop - thumb > 0 ? '│' : ''}`}
            </Text>
          ) : null}
        </>
      )}
    </Box>
  )
}

interface StatusRuleProps {
  bgCount: number
  lastTurnEndedAt?: null | number
  liveSessionCount: number
  busy: boolean
  cols: number
  cwdLabel: string
  model: string
  modelFast?: boolean
  modelReasoningEffort?: string
  indicatorStyle?: IndicatorStyle
  notice?: Notice | null
  sessionStartedAt?: null | number
  showCost: boolean
  status: string
  statusColor: string
  t: Theme
  turnStartedAt?: null | number
  usage: Usage
  voiceLabel?: string
  onSessionCountClick?: () => void
}

interface StickyPromptTrackerProps {
  messages: readonly Msg[]
  offsets: ArrayLike<number>
  onChange: (text: string) => void
  scrollRef: RefObject<ScrollBoxHandle | null>
}

interface TranscriptScrollbarProps {
  scrollRef: RefObject<ScrollBoxHandle | null>
  t: Theme
}
