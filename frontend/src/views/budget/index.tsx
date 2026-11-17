import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/budget'
const columns = [
  "方案编号",
  "预算总额",
  "渠道分配比例",
  "人群分配比例",
  "时段分配策略",
  "消耗速率上限",
  "调整周期",
  "ROI目标"
]
const actions = [
  "提交方案评审",
  "批准方案",
  "冻结方案"
]
const statuses = [
  "草稿",
  "方案评审中",
  "已批准",
  "已冻结"
]
const stats = [
  {
    "label": "预算方案总数",
    "value": 0
  },
  {
    "label": "已批准方案",
    "value": 0
  },
  {
    "label": "评审中方案",
    "value": 0
  }
]

type Row = Record<string, string | number | boolean | null>
type Detail = Row & { [key: string]: unknown }

export default function BudgetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [detailError, setDetailError] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('预算方案列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预算分配列表读取失败')
    }
  }, [filters])

  const loadDetail = useCallback(async (planNo: string | null) => {
    if (!planNo) {
      setDetail(null)
      setDetailError('')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${planNo}`)
      if (!response.ok) {
        throw new Error('预算方案明细读取失败')
      }
      setDetail(await response.json())
      setDetailError('')
    } catch (error) {
      setDetail(null)
      setDetailError(error instanceof Error ? error.message : '预算方案明细读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void loadDetail(selectedId)
  }, [selectedId, loadDetail])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row['方案编号'] ?? row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (payload.ok === false) {
        setErrorMessage(payload.message ?? '预算分配动作未生效')
        return
      }
      setNotice(payload.message ?? '')
      await reload()
      if (selectedId) {
        await loadDetail(selectedId)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '预算分配操作失败')
    }
  }

  async function runCommand(path: string, body?: Record<string, unknown>) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify(body ?? {}),
      })
      const payload = await response.json()
      if (payload.ok === false) {
        setErrorMessage(payload.message ?? '操作未生效')
        return
      }
      setNotice(payload.message ?? '')
      await reload()
      if (selectedId) {
        await loadDetail(selectedId)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '操作失败')
    }
  }

  return (
    <section className="page" data-module="budget">
      <header className="page-head">
        <div>
          <h2>预算分配管理</h2>
          <p className="page-desc">管理运营预算的分配方案，按渠道、人群、时段分配并对齐ROI目标。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('预算方案登记入口尚未接入审批流')}>
            登记预算方案
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出预算分配清单
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
          <span className="stat-label">本轮复核对象</span>
          <strong className="stat-value">BUDG-0001</strong>
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
            <th>价值来源</th>
            <th>复核状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const planNo = String(row['方案编号'] ?? '')
            return (
              <tr key={String(row.id)} className={selectedId === planNo ? 'row-selected' : undefined}>
                {columns.map((column) => (
                  <td key={column}>
                    {column === '方案编号' ? (
                      <button className="link" type="button" onClick={() => setSelectedId(planNo)}>
                        {row[column] ?? '—'}
                      </button>
                    ) : (row[column] ?? '—')}
                  </td>
                ))}
                <td className={row['来源有效'] === false ? 'error-text' : undefined}>
                  {row['来源有效'] === false ? `来源失效：${row['来源提示'] ?? '渠道额度引用了不可用模型'}` : '有效'}
                </td>
                <td>{String(row['复核状态'] ?? '—')}</td>
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
              <td className="empty-state" colSpan={columns.length + 3}>暂无预算分配数据，可先登记预算方案</td>
            </tr>
          )}
        </tbody>
      </table>

      {detailError && <p className="error-text">{detailError}</p>}
      {detail && <PlanDetail detail={detail} onCommand={runCommand} />}

      <footer className="page-foot">
        <span>共 {total} 条预算分配记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}

type Chain = {
  模型?: Record<string, string | number | boolean | null>[]
  用户价值记录?: Record<string, string | number | boolean | null>[]
  渠道分配记录?: Record<string, string | number | boolean | null>[]
  断点?: Record<string, string | number | boolean | null>[]
}

function PlanDetail({ detail, onCommand }: {
  detail: Detail
  onCommand: (path: string, body?: Record<string, unknown>) => Promise<void>
}) {
  const chain = (detail['价值链路'] ?? {}) as Chain
  const allocations = (detail['当前渠道额度'] ?? []) as Record<string, string | number | boolean | null>[]
  const datasets = (detail['数据集'] ?? []) as Record<string, string | number | boolean | null>[]
  const material = (detail['业务版本材料'] ?? null) as Record<string, unknown> | null
  const planNo = String(detail['方案编号'] ?? '')
  const [businessVersion, setBusinessVersion] = useState('')
  const [uploadText, setUploadText] = useState('')

  async function submitUpload() {
    let items: unknown = []
    try {
      items = JSON.parse(uploadText || '[]')
    } catch {
      items = []
    }
    await onCommand(`/api/budget/${planNo}/datasets`, {
      业务版本: businessVersion || (material?.['确认版本'] as string) || undefined,
      渠道: items,
    })
  }

  return (
    <section className="detail-panel">
      <h3>预算方案 {planNo} · 价值来源链路</h3>
      <p className="page-desc">
        调用链版本 {String(detail['调用链版本'] ?? '—')}；业务版本 {String(detail['业务版本'] ?? '—')}；
        复核版本 {String(detail['复核版本'] ?? '—')}；结果版本 {String(detail['数据集版本'] ?? '—')}
        {material ? `；材料 ${String(material['材料编号'])}（${String(material['材料状态'])}，确认 ${String(material['确认版本'])}）` : ''}
      </p>
      {detail['来源有效'] === false && (
        <p className="alert-line">价值来源失效：{String(detail['来源提示'] ?? '渠道额度引用了不可用模型')}；已停止批准。</p>
      )}

      <h4>LTV模型链路</h4>
      <table className="data-table">
        <thead>
          <tr><th>模型编号</th><th>模型名称</th><th>当前状态</th><th>用户价值合计</th><th>参与重算</th><th>说明</th></tr>
        </thead>
        <tbody>
          {(chain.模型 ?? []).map((item) => (
            <tr key={String(item['模型编号'])}>
              <td>{String(item['模型编号'])}</td>
              <td>{String(item['模型名称'] ?? '—')}</td>
              <td>{String(item['状态'])}</td>
              <td>{String(item['用户价值合计'])}</td>
              <td>{item['参与重算'] ? '是' : '否'}</td>
              <td>{String(item['排除原因'] || '—')}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>当前渠道额度</h4>
      <table className="data-table">
        <thead>
          <tr><th>分配编号</th><th>渠道</th><th>模型编号</th><th>渠道额度</th><th>原渠道额度</th><th>ROI偏差</th><th>来源状态</th><th>生效状态</th></tr>
        </thead>
        <tbody>
          {allocations.map((row) => (
            <tr key={String(row['分配编号'])}>
              <td>{String(row['分配编号'])}</td>
              <td>{String(row['渠道'])}</td>
              <td>{String(row['模型编号'] ?? '—')}</td>
              <td>{String(row['渠道额度'])}</td>
              <td>{String(row['原渠道额度'])}</td>
              <td>{String(row['ROI偏差'] ?? '—')}</td>
              <td>{String(row['来源状态'])}</td>
              <td>{String(row['生效状态'] ?? '—')}</td>
            </tr>
          ))}
          {allocations.length === 0 && (
            <tr><td className="empty-state" colSpan={8}>当前没有渠道额度</td></tr>
          )}
        </tbody>
      </table>

      <h4>断点（按调用链版本 {String(detail['调用链版本'] ?? '—')} 核对）</h4>
      <table className="data-table">
        <thead>
          <tr><th>数据编号</th><th>数据集编号</th><th>断点类型</th><th>模型编号</th><th>模型状态</th><th>渠道</th><th>渠道额度</th></tr>
        </thead>
        <tbody>
          {(chain.断点 ?? []).map((row, index) => (
            <tr key={`${String(row['数据编号'])}-${index}`}>
              <td>{String(row['数据编号'])}</td>
              <td>{String(row['数据集编号'])}</td>
              <td>{String(row['断点类型'])}</td>
              <td>{String(row['模型编号'])}</td>
              <td>{String(row['模型状态'])}</td>
              <td>{String(row['渠道'])}</td>
              <td>{String(row['渠道额度'])}</td>
            </tr>
          ))}
          {(chain.断点 ?? []).length === 0 && (
            <tr><td className="empty-state" colSpan={7}>当前链路没有断点</td></tr>
          )}
        </tbody>
      </table>

      <h4>渠道预算分配明细数据集</h4>
      <table className="data-table">
        <thead>
          <tr><th>数据集编号</th><th>数据集版本</th><th>状态</th><th>来源状态</th><th>条目数</th><th>渠道额度合计</th><th>模型范围</th><th>核对口径</th></tr>
        </thead>
        <tbody>
          {datasets.map((row) => (
            <tr key={String(row['数据集编号'])}>
              <td>{String(row['数据集编号'])}</td>
              <td>{String(row['数据集版本'])}</td>
              <td>{String(row['状态'])}</td>
              <td className={row['来源状态'] === '失效' ? 'error-text' : undefined}>
                {String(row['来源状态'] ?? '—')}
                {row['失效模型'] ? `（${String(row['失效模型'])}）` : ''}
              </td>
              <td>{String(row['条目数'] ?? '—')}</td>
              <td>{String(row['渠道额度合计'] ?? '—')}</td>
              <td>{Array.isArray(row['模型范围']) ? (row['模型范围'] as string[]).join('、') : '—'}</td>
              <td>{String(row['核对口径'] ?? '现行口径')}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="detail-actions">
        <button className="btn primary" type="button" onClick={() => void onCommand(`/api/budget/${planNo}/recalculate`)}>
          按模型状态重算渠道额度
        </button>
        <button className="btn" type="button" onClick={() => void onCommand(`/api/budget/${planNo}/deliver`)}>
          投递到LTV估算待处理明细
        </button>
        <button className="btn" type="button" onClick={() => void onCommand(`/api/budget/${planNo}/package`)}>
          打包渠道预算分配明细数据集
        </button>
      </div>

      <h4>上传渠道预算分配明细数据集</h4>
      <div className="detail-actions">
        <label className="filter-item">
          <span>业务版本</span>
          <input
            value={businessVersion}
            placeholder={String(material?.['确认版本'] ?? '以业务版本材料为准')}
            onChange={(event) => setBusinessVersion(event.target.value)}
          />
        </label>
        <label className="filter-item grow">
          <span>渠道条目（JSON 数组：渠道、模型编号、渠道额度）</span>
          <textarea
            rows={3}
            value={uploadText}
            onChange={(event) => setUploadText(event.target.value)}
            placeholder='[{"渠道":"搜索","模型编号":"LIFE-0003","渠道额度":300000}]'
          />
        </label>
        <button className="btn" type="button" onClick={() => void submitUpload()}>上传并校验</button>
      </div>
    </section>
  )
}
