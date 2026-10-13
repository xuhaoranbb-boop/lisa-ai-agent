import assert from 'node:assert/strict';
import {
  contextPanelRows,
  executionPanelRows,
  healthPanelRows,
  memoryPanelRows,
  performancePanelRows,
  screenPanelRows,
  systemPanelRows,
  tracePanelRows,
  workflowPanelRows,
  taskPanelRows
} from '../src/renderer/runtime-panels.ts';

assert.deepEqual(
  memoryPanelRows([
    {
      id: 1,
      content: '用户偏好简洁回答',
      category: 'preference',
      importance: 4,
      confidence: 0.9,
      summary: '',
      updated_at: '2026-09-12T00:00:00+00:00'
    }
  ]),
  ['preference · 90% · 用户偏好简洁回答']
);

assert.deepEqual(
  taskPanelRows([
    {
      id: 'task-1',
      goal: '检查项目',
      status: 'EXECUTING',
      progress: 0.5,
      plan: []
    }
  ]),
  ['EXECUTING · 50% · 检查项目']
);

assert.deepEqual(
  contextPanelRows({
    goal: '检查项目',
    summary: '已经完成测试',
    unresolved_references: [{ phrase: '刚才那个', value: '测试报告' }]
  }),
  ['目标: 检查项目', '摘要: 已经完成测试', '引用: 刚才那个 -> 测试报告']
);

assert.deepEqual(
  executionPanelRows({
    execution_id: 'e1',
    task_id: 't1',
    goal: '检查项目',
    status: 'RUNNING',
    current_step: 's1',
    result: '',
    error: '',
    created_at: '2026-09-12T00:00:00+00:00',
    updated_at: '2026-09-12T00:00:00+00:00',
    steps: [
      {
        step_id: 's1',
        title: '运行测试',
        tool: 'run_workspace_command',
        status: 'RUNNING',
        result: '',
        error: '',
        retry_count: 1
      }
    ]
  }),
  ['RUNNING · 运行测试 · run_workspace_command · retry=1']
);

assert.deepEqual(
  screenPanelRows({
    application: 'Code',
    window: 'main.py',
    confidence: 0.9,
    errors: ['ImportError'],
    buttons: ['Run']
  }),
  ['APPLICATION: Code', 'WINDOW: main.py', 'CONFIDENCE: 90%', 'ERRORS: ImportError', 'BUTTONS: Run']
);

assert.deepEqual(
  systemPanelRows({
    cpu_count: 16,
    memory_percent: 72,
    disk_free_gb: 120,
    services: { llm: true, mcp: false }
  }),
  ['CPU: 16 CORES', 'RAM: 72%', 'DISK FREE: 120 GB', 'LLM: ONLINE', 'MCP: OFFLINE']
);

assert.deepEqual(
  workflowPanelRows({
    workflow_id: 'w1',
    name: 'report',
    status: 'RUNNING',
    steps: [{ id: 's1', title: '读取文件', status: 'RUNNING', tool: 'file_read' }]
  }),
  ['RUNNING · 读取文件 · file_read']
);

assert.deepEqual(
  tracePanelRows([
    {
      id: 1,
      event_type: 'TOOL_RESULT',
      tool: 'get_current_time',
      model: '',
      latency_ms: 12,
      input_tokens: 3,
      output_tokens: 5,
      estimated_cost: 0,
      error: '',
      created_at: '2026-09-12T00:00:00+00:00'
    }
  ]),
  ['TOOL_RESULT · get_current_time · 12ms · 8 tokens']
);

assert.deepEqual(
  healthPanelRows({
    status: 'DEGRADED',
    components: [
      { name: 'llm', status: 'HEALTHY' },
      { name: 'mcp', status: 'OFFLINE', message: 'disconnected' }
    ]
  }),
  ['OVERALL: DEGRADED', 'LLM: HEALTHY', 'MCP: OFFLINE · disconnected']
);

assert.deepEqual(
  performancePanelRows(
    [
      {
        id: 1,
        event_type: 'LLM_FIRST_TOKEN',
        tool: '',
        model: 'deepseek-chat',
        latency_ms: 100,
        input_tokens: 10,
        output_tokens: 20,
        estimated_cost: 0.001,
        error: '',
        created_at: '2026-09-12T00:00:00+00:00'
      }
    ],
    {
      cpu_count: 8,
      memory_percent: 50,
      disk_free_gb: 100
    }
  ),
  [
    'AVG LATENCY: 100 ms',
    'TRACE TOKENS: 30',
    'TRACE COST: $0.001000',
    'CPU: 8 CORES',
    'RAM: 50%',
    'DISK FREE: 100 GB'
  ]
);

console.log('runtime panel model tests passed');
