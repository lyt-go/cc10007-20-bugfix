import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/lifetime'
const columns = [
  "模型编号",
  "模型名称",
  "估算方法",
  "观察窗口天",
  "预测窗口天",
  "折现率",
  "客单价因子",
  "留存率因子"
]
const actions = [
  "提交模型校验",
  "启用模型",
  "废弃模型"
]
const statuses = [
  "草稿",
  "模型校验中",
  "已启用",
  "已废弃"
]
const stats = [
  {
    "label": "LTV模型总数",
    "value": 0
  },
  {
    "label": "已启用模型",
    "value": 0
  },
  {
    "label": "校验中模型",
    "value": 0
  }
]

type Row = Record<string, string | number | boolean | null>
type Reference = Record<string, string | number | boolean | null>

export default function LifetimeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Row | null>(null)
  const [detailError, setDetailError] = useState('')
  const [pending, setPending] = useState<Row[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('LTV模型列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'LTV估算列表读取失败')
    }
  }, [filters])

  const reloadPending = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/pending`)
      if (!response.ok) {
        throw new Error('LTV估算待处理明细读取失败')
      }
      const payload = await response.json()
      setPending(payload.items ?? [])
    } catch {
      setPending([])
    }
  }, [])

  const loadDetail = useCallback(async (modelNo: string | null) => {
    if (!modelNo) {
      setDetail(null)
      setDetailError('')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${modelNo}`)
      if (!response.ok) {
        throw new Error('LTV模型明细读取失败')
      }
      setDetail(await response.json())
      setDetailError('')
    } catch (error) {
      setDetail(null)
      setDetailError(error instanceof Error ? error.message : 'LTV模型明细读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadPending()
  }, [reload, reloadPending])

  useEffect(() => {
    void loadDetail(selectedId)
  }, [selectedId, loadDetail])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row['模型编号'] ?? row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (payload.ok === false) {
        setErrorMessage(payload.message ?? 'LTV估算动作未生效')
        return
      }
      setNotice(payload.message ?? '')
      await reload()
      await reloadPending()
      if (selectedId) {
        await loadDetail(selectedId)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'LTV估算操作失败')
    }
  }

  async function archivePending(row: Row) {
    try {
      const response = await request(`${ENDPOINT}/pending/${row['待处理编号']}/archive`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      const payload = await response.json()
      if (payload.ok === false) {
        setErrorMessage(payload.message ?? '归档未生效')
        return
      }
      setNotice(payload.message ?? '')
      await reloadPending()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '归档失败')
    }
  }

  return (
    <section className="page" data-module="lifetime">
      <header className="page-head">
        <div>
          <h2>LTV估算管理</h2>
          <p className="page-desc">管理用户生命周期价值的估算模型，配置观察窗口和预测参数。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('LTV模型登记入口尚未接入审批流')}>
            登记LTV模型
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出LTV估算清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
        <article className="stat-card">
          <span className="stat-label">待处理明细</span>
          <strong className="stat-value">{pending.length}</strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>预算引用</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const modelNo = String(row['模型编号'] ?? '')
            const refs = Array.isArray(row['预算引用']) ? (row['预算引用'] as Reference[]) : []
            return (
              <tr key={String(row.id)} className={selectedId === modelNo ? 'row-selected' : undefined}>
                {columns.map((column) => (
                  <td key={column}>
                    {column === '模型编号' ? (
                      <button className="link" type="button" onClick={() => setSelectedId(modelNo)}>
                        {row[column] ?? '—'}
                      </button>
                    ) : (row[column] ?? '—')}
                  </td>
                ))}
                <td className={refs.some((item) => item['来源有效'] === false) ? 'error-text' : undefined}>
                  {refs.length === 0
                    ? '—'
                    : refs.map((item) => `${String(item['方案编号'])}${item['来源有效'] === false ? '（来源失效）' : ''}`).join('、')}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无LTV估算数据，可先登记LTV模型</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">LTV估算待处理明细</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>待处理编号</th><th>方案编号</th><th>数据集编号</th><th>数据集版本</th>
            <th>业务版本</th><th>调用链版本</th><th>模型范围</th><th>渠道额度合计</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {pending.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row['待处理编号'])}</td>
              <td>{String(row['方案编号'])}</td>
              <td>{String(row['数据集编号'])}</td>
              <td>{String(row['数据集版本'])}</td>
              <td>{String(row['业务版本'] ?? '—')}</td>
              <td>{String(row['调用链版本'] ?? '—')}</td>
              <td>{Array.isArray(row['模型范围']) ? (row['模型范围'] as string[]).join('、') : '—'}</td>
              <td>{String(row['渠道额度合计'] ?? '—')}</td>
              <td>{String(row['状态'])}</td>
              <td>
                {row['状态'] === '待处理' && (
                  <button className="link" type="button" onClick={() => void archivePending(row)}>来源失效归档</button>
                )}
              </td>
            </tr>
          ))}
          {pending.length === 0 && (
            <tr><td className="empty-state" colSpan={10}>暂无待处理明细</td></tr>
          )}
        </tbody>
      </table>

      {detailError && <p className="error-text">{detailError}</p>}
      {detail && <ModelDetail detail={detail} />}

      <footer className="page-foot">
        <span>共 {total} 条LTV估算记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}

function ModelDetail({ detail }: { detail: Row }) {
  const refs = (detail['预算引用'] ?? []) as Record<string, unknown>[]
  const values = (detail['用户价值记录'] ?? []) as Row[]
  return (
    <section className="detail-panel">
      <h3>LTV模型 {String(detail['模型编号'])} · 价值来源去向</h3>
      <p className="page-desc">
        当前状态 {String(detail['status'])}；模型版本 {String(detail['模型版本'] ?? '—')}；
        价值记录版本 {String(detail['价值记录版本'] ?? '—')}；状态版本 {String(detail['状态版本'] ?? '—')}
      </p>
      <h4>用户价值记录</h4>
      <table className="data-table">
        <thead><tr><th>记录编号</th><th>渠道</th><th>用户分群</th><th>用户数</th><th>用户价值</th><th>状态</th></tr></thead>
        <tbody>
          {values.map((row) => (
            <tr key={String(row['记录编号'])}>
              <td>{String(row['记录编号'])}</td>
              <td>{String(row['渠道'])}</td>
              <td>{String(row['用户分群'] ?? '—')}</td>
              <td>{String(row['用户数'] ?? '—')}</td>
              <td>{String(row['用户价值'])}</td>
              <td>{String(row['状态'])}</td>
            </tr>
          ))}
          {values.length === 0 && <tr><td className="empty-state" colSpan={6}>暂无用户价值记录</td></tr>}
        </tbody>
      </table>

      <h4>预算方案引用</h4>
      <table className="data-table">
        <thead>
          <tr><th>方案编号</th><th>方案状态</th><th>复核状态</th><th>数据集版本</th><th>来源状态</th><th>渠道额度</th></tr>
        </thead>
        <tbody>
          {refs.map((row) => (
            <tr key={String(row['方案编号'])}>
              <td>{String(row['方案编号'])}</td>
              <td>{String(row['status'] ?? '—')}</td>
              <td>{String(row['复核状态'] ?? '—')}</td>
              <td>{String(row['数据集版本'] ?? '—')}</td>
              <td className={row['来源有效'] === false ? 'error-text' : undefined}>
                {row['来源有效'] === false ? `来源失效：${String(row['来源提示'] ?? '')}` : '有效'}
              </td>
              <td>
                {((row['渠道额度'] ?? []) as Record<string, unknown>[]).map((item) => (
                  <span key={String(item['分配编号'])} className="quota-chip">
                    {String(item['渠道'])} {String(item['渠道额度'])}
                    {item['历史标记'] ? '（历史）' : ''}
                  </span>
                ))}
              </td>
            </tr>
          ))}
          {refs.length === 0 && <tr><td className="empty-state" colSpan={6}>没有预算方案引用该模型</td></tr>}
        </tbody>
      </table>
    </section>
  )
}
