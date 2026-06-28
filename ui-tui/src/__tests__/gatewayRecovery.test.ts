import { describe, expect, it } from 'vitest'

import { GATEWAY_RECOVERY_LIMIT, GATEWAY_RECOVERY_WINDOW_MS, planGatewayRecovery } from '../app/gatewayRecovery.js'

describe('planGatewayRecovery', () => {
  it('recovers the live session and records the attempt', () => {
    const plan = planGatewayRecovery('sess-1', null, [], 1000)

    expect(plan).toEqual({ attempts: [1000], recover: true, sid: 'sess-1' })
  })

  it('does not recover when there is no session to resume', () => {
    expect(planGatewayRecovery(null, null, [], 1000)).toEqual({ attempts: [], recover: false, sid: null })
  })

  it('keeps retrying the recovery target through a startup crash-loop, bounded by the budget', () => {
    // 第一次退出：存在活跃 sid。
    let attempts: number[] = []
    let plan = planGatewayRecovery('sess-1', null, attempts, 0)

    expect(plan.recover).toBe(true)
    expect(plan.sid).toBe('sess-1')
    attempts = plan.attempts

    // 在 gateway.ready 之前 respawn 循环崩溃：活跃 sid 现在为 null，但
    // 恢复目标会将其传递下去，因此我们持续尝试直到预算耗尽。
    for (let i = 1; i < GATEWAY_RECOVERY_LIMIT; i++) {
      plan = planGatewayRecovery(null, 'sess-1', attempts, i)
      expect(plan.recover).toBe(true)
      expect(plan.sid).toBe('sess-1')
      attempts = plan.attempts
    }

    // 预算耗尽：回退到非活跃状态，而非风暴式 spawn。
    plan = planGatewayRecovery(null, 'sess-1', attempts, GATEWAY_RECOVERY_LIMIT)
    expect(plan.recover).toBe(false)
    expect(plan.sid).toBe('sess-1')
  })

  it('prunes attempts older than the window so recovery re-arms', () => {
    const old = Array.from({ length: GATEWAY_RECOVERY_LIMIT }, (_, i) => i)
    const plan = planGatewayRecovery('sess-1', null, old, GATEWAY_RECOVERY_WINDOW_MS + 100)

    expect(plan.attempts).toEqual([GATEWAY_RECOVERY_WINDOW_MS + 100])
    expect(plan.recover).toBe(true)
  })
})
