import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// memory.js 会执行真实的堆转储 / fs 操作 —— 将其 stub 掉，
// 使 monitor 的 dump 路径在测试中为空操作。
vi.mock('../lib/memory.js', () => ({
  performHeapDump: vi.fn(async () => null)
}))

// @hermes/ink 仅在 dump 路径上动态导入；stub 掉缓存清理。
vi.mock('@hermes/ink', () => ({ evictInkCaches: vi.fn() }))

import { startMemoryMonitor } from '../lib/memoryMonitor.js'

const GB = 1024 ** 3
const MB = 1024 ** 2

describe('startMemoryMonitor thresholds (#34095)', () => {
  let stop: (() => void) | undefined

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    stop?.()
    stop = undefined
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  const withHeap = (heapUsed: number, rss = heapUsed) =>
    vi.spyOn(process, 'memoryUsage').mockReturnValue({
      arrayBuffers: 0,
      external: 0,
      heapTotal: heapUsed,
      heapUsed,
      rss
    } as NodeJS.MemoryUsage)

  it('does NOT fire onCritical at 2.5GB when the heap ceiling is 8GB', async () => {
    // 旧的硬编码 2.5GB 常量在达到实际~31% 上限时就杀死了进程。
    // 使用相对阈值（~88%），2.5GB 远在正常范围内。
    const onCritical = vi.fn()
    withHeap(2.5 * GB)
    stop = startMemoryMonitor({ criticalBytes: 7 * GB, highBytes: 5 * GB, intervalMs: 1, onCritical })

    await vi.advanceTimersByTimeAsync(5)

    expect(onCritical).not.toHaveBeenCalled()
  })

  it('fires onCritical only near the configured ceiling', async () => {
    const onCritical = vi.fn()
    // 通过 override 使用显式的小上限派生阈值，使测试
    // 不依赖于宿主 V8 的 heap_size_limit。
    withHeap(7.5 * GB)
    stop = startMemoryMonitor({ criticalBytes: 7 * GB, highBytes: 5 * GB, intervalMs: 1, onCritical })

    await vi.advanceTimersByTimeAsync(5)

    expect(onCritical).toHaveBeenCalledTimes(1)
  })

  it('fires onWarn once on fast sub-threshold heap growth, then re-arms', async () => {
    const onWarn = vi.fn()
    // 从低值开始，然后在一次 tick 内跳增 >150MB，同时高于 600MB 下限且
    // 低于 `high` —— 即静默死亡区间。
    const spy = withHeap(100 * MB)
    stop = startMemoryMonitor({ highBytes: 2 * GB, intervalMs: 1, onWarn, warnBytes: 600 * MB })

    await vi.advanceTimersByTimeAsync(2) // 将 lastHeap 设为 100MB，低于下限
    expect(onWarn).not.toHaveBeenCalled()

    spy.mockReturnValue({ arrayBuffers: 0, external: 0, heapTotal: 800 * MB, heapUsed: 800 * MB, rss: 800 * MB } as NodeJS.MemoryUsage)
    await vi.advanceTimersByTimeAsync(2) // 跳增 700MB → 超过下限 + 陡峭增长
    expect(onWarn).toHaveBeenCalledTimes(1)

    // 保持高位但不再重复触发。
    await vi.advanceTimersByTimeAsync(2)
    expect(onWarn).toHaveBeenCalledTimes(1)

    // 回落到下限以下 → 重新激活，然后再次攀升 → 再次触发。
    spy.mockReturnValue({ arrayBuffers: 0, external: 0, heapTotal: 100 * MB, heapUsed: 100 * MB, rss: 100 * MB } as NodeJS.MemoryUsage)
    await vi.advanceTimersByTimeAsync(2)
    spy.mockReturnValue({ arrayBuffers: 0, external: 0, heapTotal: 800 * MB, heapUsed: 800 * MB, rss: 800 * MB } as NodeJS.MemoryUsage)
    await vi.advanceTimersByTimeAsync(2)
    expect(onWarn).toHaveBeenCalledTimes(2)
  })

  it('does not warn on slow growth below the steep-growth step', async () => {
    const onWarn = vi.fn()
    const spy = withHeap(650 * MB)
    stop = startMemoryMonitor({ highBytes: 2 * GB, intervalMs: 1, onWarn, warnBytes: 600 * MB })

    await vi.advanceTimersByTimeAsync(2)
    // +50MB/tick —— 高于下限但平缓，不是渲染树爆炸。
    spy.mockReturnValue({ arrayBuffers: 0, external: 0, heapTotal: 700 * MB, heapUsed: 700 * MB, rss: 700 * MB } as NodeJS.MemoryUsage)
    await vi.advanceTimersByTimeAsync(2)

    expect(onWarn).not.toHaveBeenCalled()
  })
})
