# 智能运营决策分析平台

面向用户行为序列分析、RFM分层、流失预警、智能推荐、归因建模、活动效果评估与实时运营决策的数据分析工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 行为序列 | `sequence` | 序列模式 | 模式编号、用户分群、事件序列 |
| RFM分层 | `rfm` | 分层规则 | 规则编号、最近消费天界、消费频次界 |
| 流失预警 | `churn` | 预警模型 | 模型编号、模型名称、特征变量 |
| 智能推荐 | `recommend` | 推荐策略 | 策略编号、推荐场景、召回算法 |
| 归因建模 | `attrmodel` | 归因模型 | 模型编号、模型名称、归因逻辑 |
| 活动评估 | `activity` | 活动方案 | 方案编号、活动名称、活动类型 |
| 实时决策 | `realtime` | 决策规则 | 规则编号、触发事件、决策条件 |
| 预算分配 | `budget` | 预算方案 | 方案编号、预算总额、渠道分配比例 |
| LTV估算 | `lifetime` | LTV模型 | 模型编号、模型名称、估算方法 |
| 实验变体 | `abvariant` | 变体方案 | 变体编号、关联实验、变体名称 |
| 运营看板 | `dashboard` | 看板视图 | 视图编号、视图名称、指标组合 |
| 评分卡 | `scorecard` | 评分卡 | 评分编号、评分对象、特征变量组 |
| 聚类分群 | `cluster` | 聚类任务 | 任务编号、聚类算法、特征维度 |
| 对比分析 | `comparison` | 对比组 | 对比编号、对比名称、实验组筛选 |
| 数据漂移 | `datadrift` | 漂移检测 | 检测编号、监控数据集、基线版本 |
| 推送计划 | `pushplan` | 推送方案 | 方案编号、推送内容、目标人群 |
| 指标归因 | `metricdrill` | 归因分析 | 分析编号、目标指标、波动方向 |
| 策略库 | `tacticlib` | 运营策略 | 策略编号、策略名称、策略类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 预算分配与LTV估算的联动

预算分配与LTV估算的跨模块规则收在 `backend/app/services/allocation.py`，示例数据补在
`backend/app/domain_seed.py`。关键口径：

- **价值来源只看“已启用”模型**：模型校验中、草稿、已废弃的LTV一律不参与渠道额度重算；
  `LIFE-0002` 处于模型校验中，遗留数据集 `DSET-0001` 仍为它保留信息流额度，这就是
  `BUDG-0001`（本轮复核对象）的来源失效断点。
- **结果版本由业务版本材料确认**：`business_version` 表里的材料确认哪个版本，重算出的
  渠道预算分配明细数据集就落在哪个业务版本下（`BUDG-0001-BV-2026-11-A-V2`）。
- **模型状态与渠道额度同批落定**：LTV 废弃/启用会同步改写引用它的渠道额度、方案来源状态
  和当前数据集来源状态，原渠道额度与ROI偏差保留为历史，可按分配编号复查。
- **同一业务记录只允许一个处理终点**：批准、冻结、LTV下线回执、文件接收共用一把记录级锁；
  并发上传只接纳一个来源有效的渠道预算分配明细数据集（其余按幂等或冲突拒绝）。
- **已批准的历史分配保持原预算时点状态**：批准次数与批准时点不变，后续废弃只把当前额度
  置零并标记来源失效待复核。

相关接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/budget/{方案}/audit?chain_version=` | 按遗留调用链版本核对断点与检查项 |
| `POST /api/budget/{方案}/recalculate` | 按模型状态重算渠道额度并生成数据集 |
| `POST /api/budget/{方案}/datasets` | 接纳上传的渠道预算分配明细数据集 |
| `POST /api/budget/{方案}/deliver` | 投递到LTV估算待处理明细 |
| `POST /api/budget/{方案}/package` `/receive` | 打包与文件接收 |
| `POST /api/budget/{方案}/value-version` | 替换为新价值版本并重算 |
| `GET /api/lifetime/pending` | LTV估算待处理明细 |

验收脚本（覆盖校验中参与、废弃残留、重复批准、失败回退）：

```bash
cd backend && .venv/bin/python -m app.checks
```

领域：数据工程
子方向：数据分析与数据应用
