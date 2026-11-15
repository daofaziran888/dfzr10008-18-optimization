<template>
  <section class="page" data-module="usage">
    <header class="page-head">
      <div>
        <h2>使用效果追踪管理</h2>
        <p class="page-desc">维护使用效果追踪的调用统计、性能指标与周期汇总；列表、汇总、分析与周期快照共用同一过滤范围。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记使用记录</button>
        <button class="btn" type="button" @click="exportRows">导出使用效果追踪清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="page-actions" style="margin: 12px 0">
      <button class="btn primary" type="button" @click="summarize">汇总数据</button>
      <button class="btn" type="button" @click="analyze">分析效果</button>
      <button class="btn ghost" type="button" @click="loadSnapshot">查看周期快照</button>
      <span style="margin-left: 8px; color: #888">三个动作都使用上方同一过滤范围（产品 / 调用方 / 统计周期）</span>
    </div>

    <div v-if="snapshot" class="stat-row">
      <article class="stat-card">
        <span class="stat-label">调用次数（明细聚合）</span>
        <strong class="stat-value">{{ snapshot['调用次数'] }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">错误次数</span>
        <strong class="stat-value">{{ snapshot['错误次数'] }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">平均耗时</span>
        <strong class="stat-value">{{ snapshot['平均耗时'] }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">明细条数 / 快照版本</span>
        <strong class="stat-value">{{ snapshot['明细条数'] }} / v{{ snapshot['版本'] }}</strong>
      </article>
    </div>
    <div v-if="analysis" class="stat-card" style="margin-bottom: 12px">
      <span class="stat-label">效果分析（读取自统计周期快照）</span>
      <p style="margin: 6px 0 0">
        错误率 {{ analysis['错误率'] }}（{{ analysis['错误次数'] }}/{{ analysis['调用次数'] }}），
        周期 {{ analysis['统计周期'] }} —— {{ analysis['结论'] }}
      </p>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">当前过滤范围暂无使用记录，请调整产品 / 调用方 / 统计周期</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>当前范围共 {{ total }} 条使用效果追踪记录</span>
      <span v-if="scopeNote" class="error-text">{{ scopeNote }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Snapshot = Record<string, string | number | null | Record<string, unknown>>

const ENDPOINT = '/api/usage'
const columns = ['记录编号', '产品名称', '调用方名称', '调用次数', '平均耗时', '错误次数', '统计周期', '记录状态']
const actions = ['汇总数据', '分析效果', '归档记录']
const stats = [{ label: '使用记录总数', value: 0 }, { label: '已分析记录', value: 0 }, { label: '已归档记录', value: 0 }]

// 三个过滤字段即后端统一过滤口径：产品名称、调用方名称、统计周期
const filterFields = ['产品名称', '调用方名称', '统计周期']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const scopeNote = ref('')
const filters = ref<Record<string, string>>({})
const snapshot = ref<Snapshot | null>(null)
const analysis = ref<Record<string, unknown> | null>(null)

function scopeQuery(): URLSearchParams {
  return new URLSearchParams({
    product: filters.value['产品名称'] ?? '',
    caller: filters.value['调用方名称'] ?? '',
    period: filters.value['统计周期'] ?? '',
  })
}

function hasFullScope(): boolean {
  return Boolean(
    (filters.value['产品名称'] ?? '').trim()
    && (filters.value['调用方名称'] ?? '').trim()
    && (filters.value['统计周期'] ?? '').trim(),
  )
}

function resetFilters() {
  filters.value = {}
  snapshot.value = null
  analysis.value = null
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export?${scopeQuery().toString()}`, '_blank')
}

function openCreate() {
  errorMessage.value = '使用记录登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('使用效果追踪动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '使用效果追踪操作失败'
  }
}

async function summarize() {
  errorMessage.value = ''
  scopeNote.value = ''
  if (!hasFullScope()) {
    scopeNote.value = '汇总数据需要同时填写产品名称、调用方名称和统计周期'
    return
  }
  const response = await request(`${ENDPOINT}/summary?${scopeQuery().toString()}`)
  const payload = await response.json()
  snapshot.value = payload.snapshot
  scopeNote.value = payload.message
  await reload()
}

async function analyze() {
  errorMessage.value = ''
  scopeNote.value = ''
  if (!hasFullScope()) {
    scopeNote.value = '分析效果需要同时填写产品名称、调用方名称和统计周期'
    return
  }
  const response = await request(`${ENDPOINT}/analysis?${scopeQuery().toString()}`)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: '效果分析失败' }))
    scopeNote.value = payload.detail ?? '效果分析失败'
    return
  }
  const payload = await response.json()
  snapshot.value = payload.snapshot
  analysis.value = payload.analysis
  scopeNote.value = payload.message
  await reload()
}

async function loadSnapshot() {
  errorMessage.value = ''
  scopeNote.value = ''
  if (!hasFullScope()) {
    scopeNote.value = '查看快照需要同时填写产品名称、调用方名称和统计周期'
    return
  }
  const response = await request(`${ENDPOINT}/snapshots/current?${scopeQuery().toString()}`)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: '快照不存在' }))
    snapshot.value = null
    scopeNote.value = payload.detail ?? '快照不存在'
    return
  }
  const payload = await response.json()
  snapshot.value = payload.snapshot
  analysis.value = (payload.snapshot['分析结果'] as Record<string, unknown>) ?? null
  scopeNote.value = payload.message
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  for (const field of filterFields) {
    if (filters.value[field]) {
      query.set(field, filters.value[field])
    }
  }
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('使用记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '使用效果追踪列表读取失败'
  }
}

onMounted(reload)
</script>
