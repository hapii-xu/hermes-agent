import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { SessionPanel } from '../components/branding.js'
import { DEFAULT_THEME } from '../theme.js'
import type { McpServerStatus, SessionInfo } from '../types.js'

// 测试中的不变量：TUI banner 的 MCP 标题计数只统计 *已连接* 的
// server，不包括已配置但禁用的。这反映了经典 CLI
// banner（hermes_cli/banner.py 中的 `mcp_connected = sum(1 for s in mcp_status if s["connected"])`）
// 以及 MCP 折叠切换按钮上的 "connected" 标签。
//
// 回归问题：branding.tsx 曾使用原始的 `info.mcp_servers.length`，因此
// 一个禁用的 `linear` server 加上一个已连接的 `nous-support` server 会导致
// TUI 报告 "2 MCP"，而经典 CLI 正确报告 "1 MCP"。

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const makeStreams = (columns = 100) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })

  let captured = ''
  stdout.on('data', chunk => {
    captured += chunk.toString()
  })

  return { capture: () => captured, stderr, stdin, stdout }
}

const mcp = (over: Partial<McpServerStatus> & Pick<McpServerStatus, 'name'>): McpServerStatus => ({
  connected: false,
  tools: 0,
  transport: 'http',
  ...over
})

const baseInfo = (mcp_servers: McpServerStatus[]): SessionInfo => ({
  mcp_servers,
  model: 'test-model',
  skills: { core: ['a', 'b'] },
  tools: { file: ['read_file', 'write_file'] }
})

async function renderFooter(info: SessionInfo): Promise<string> {
  const streams = makeStreams()

  const instance = renderSync(React.createElement(SessionPanel, { info, sid: 'test', t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: streams.stderr as NodeJS.WriteStream,
    stdin: streams.stdin as NodeJS.ReadStream,
    stdout: streams.stdout as NodeJS.WriteStream
  })

  try {
    await delay(20)

    // 去除 ANSI 以便对渲染的文本内容进行断言。
    // eslint-disable-next-line no-control-regex
    return streams.capture().replace(/\u001b\[[0-9;]*m/g, '')
  } finally {
    instance.unmount()
    instance.cleanup()
  }
}

describe('branding MCP headline count', () => {
  it('counts only connected servers, not configured-but-disabled ones', async () => {
    const frame = await renderFooter(
      baseInfo([
        mcp({ connected: true, name: 'nous-support', status: 'connected', tools: 6 }),
        mcp({ connected: false, disabled: true, name: 'linear', status: 'disabled' })
      ])
    )

    // 一个已连接的 server → "1 MCP"，而非 "2 MCP"。
    expect(frame).toContain('1 MCP')
    expect(frame).not.toContain('2 MCP')
  })

  it('drops the MCP segment entirely when no server is connected', async () => {
    const frame = await renderFooter(
      baseInfo([mcp({ connected: false, disabled: true, name: 'linear', status: 'disabled' })])
    )

    // 与经典 CLI 一致，仅在 N > 0 时追加 "· N MCP"。
    expect(frame).not.toContain('MCP servers')
    expect(frame).not.toMatch(/\d MCP\b/)
  })

  it('counts every connected server when several are connected', async () => {
    const frame = await renderFooter(
      baseInfo([
        mcp({ connected: true, name: 'alpha', status: 'connected' }),
        mcp({ connected: true, name: 'beta', status: 'connected' }),
        mcp({ connected: false, disabled: true, name: 'gamma', status: 'disabled' })
      ])
    )

    expect(frame).toContain('2 MCP')
    expect(frame).not.toContain('3 MCP')
  })
})
