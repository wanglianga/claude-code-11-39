# 跨境出口申报与到账事实对账数据服务

外贸财务月末核对一笔出口业务时，发票有多行商品、报关单经历海关更正、税务回执按申报批次返回、
银行以一笔合并流水到账。本服务在**行级别**回答：哪部分出口事实已被申报、审核和收款证据支持，
哪些金额只是候选关联、待分摊或相互矛盾，并把每一条可审计结论追溯到来源文件。

**本服务只交付事实，不代办业务**：不提交报关或退税申报，不操作库存、税务余额或银行资金，
不生成付款、授信或税务处理指令。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 18（后端服务，带完整 OpenAPI 3.1 与 Swagger UI）。

---

## 1. 一键启动

### 1.1 Docker Compose（推荐）

```bash
docker compose up --build
```

- API：http://127.0.0.1:8000
- Swagger UI：http://127.0.0.1:8000/docs
- OpenAPI：http://127.0.0.1:8000/openapi.json
- 容器启动时自动等待 PostgreSQL、建表、播种演示数据（已存在则跳过）。

### 1.2 本地开发（容器内已装运行时）

```bash
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 本地 PostgreSQL 已在 127.0.0.1:5432 运行（库/用户/密码均为 ccdev）
.venv/bin/python scripts/wait_for_db.py
.venv/bin/python scripts/seed.py --reset          # 建表 + 演示数据 + 打印账号
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

覆盖数据库连接（默认值即为本地开发库）：

```bash
export DATABASE_URL='postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev'
```

---

## 2. 测试账号与 API Key

所有 `/api/v1/**` 接口都需要请求头 `X-API-Key: <key>`。Swagger UI 右上角 **Authorize** 填入同一个 key 即可试调。
`scripts/seed.py` 每次运行都会打印下列账号：

| 用户名 | 角色 | API Key | 授权主体 |
| --- | --- | --- | --- |
| `admin` | admin | `demo-admin-key` | 全部主体，可建主体/用户 |
| `analyst1` | analyst | `demo-analyst-1-key` | `ENT-A` |
| `analyst2` | analyst | `demo-analyst-2-key` | `ENT-A`（用于并发确认场景） |
| `entity-b-analyst` | analyst | `demo-analyst-b-key` | `ENT-B`（用于跨主体拒绝验证） |
| `viewer` | viewer | `demo-viewer-key` | `ENT-A`、`ENT-B`，只读 |

角色语义：`admin` 全权；`analyst` 可在授权主体内写；`viewer` 只读。
任何越权访问返回 **403**，缺少/错误 key 返回 **401**。

---

## 3. 领域模型（要点）

| 表 | 作用 |
| --- | --- |
| `source_documents` | **来源受理账**，append-only。保存 9 类来源（发票、装箱单、报关档案/更正、税务回执、银行流水、主体目录、合同、退运凭证、其他）的原文位置、事件时间、接收时间、外部编号、币种、金额、数量、SHA-256、来源版本、修订指向。 |
| `invoice_lines` / `customs_lines` / `packing_lines` / `tax_lines` / `bank_transactions` | 文档行。`customs_lines.revises_id` 构成**修订链**，`is_current` 标记当前有效行，`changed_fields` 记录变更前后值。 |
| `export_lines` | **原出口行**：发票行与当前报关行的规范合并行，是行级对账的单位。 |
| `declaration_ledgers` | **申报状态账**：`draft / submitted / verified / rejected / correction_pending`。 |
| `receipt_ledgers` | **到账状态账**：`unmatched / pending_allocation / partial / matched / conflict`。 |
| `balances` + `balance_holds` | **余额控制器**：银行流水侧与原出口行侧各一行（`total = committed + available`，CHECK 约束），每次占用/冻结/释放/转移都写入 `balance_holds` 流水。 |
| `dossiers` / `dossier_members` | 行级身份解析器产出的**带证据等级（A/B/C）的候选案卷**。 |
| `reconciliation_candidates` | 候选关联（`unique_reference` 或 `constrained_allocation`），**不占用余额**。 |
| `reconciliation_lines` | **已证实对账行**，只能由余额控制器创建；状态 `active / needs_review / voided / reassigned`。 |
| `difference_cases` | 差异原因（超额尝试、回执乱序、更正后复核、占用超更正金额、装箱数量不符…）。 |
| `compile_runs` | 事实编译快照，保存已证实事实、未分摊余额与差异原因。 |

**两个状态账不共享任何已结清字段**：`declaration_ledgers` 与 `receipt_ledgers` 除主键/版本/更新时间外无同名业务列，
状态词表互斥，且都不存在 `settled` 字段；结清事实只存在于 `reconciliation_lines`。`tests/test_acceptance.py::test_settlement_ledgers_do_not_share_settled_fields` 在 schema 层断言这一点。

### 处理顺序（强约束）

1. **先确认唯一关联**：付款附言含 `SKU-xxxx` / `INV-.../n` / `DECL-.../L<n>` 等行级唯一键时，直接建立关联，速度快、复核少。
2. **再对合并流水生成人工确认候选**：无唯一批次键时，按主体 + 币种 + 合同 + 出口日期（≤ 到账日）+ 已审核税务回执列出受约束候选。
3. **未确认金额永远留在待分摊余额**中；**绝不**按金额比例或行数自动分摊。
4. **并发确认**时控制器在同一事务中按 `(side, ref_id)` 字典序 `FOR UPDATE` 锁定两侧余额行，
   校验 `amount ≤ min(银行可用, 出口行可用)` 后原子占用；任一越界返回 409 并记录 `over_allocation_attempt`。
5. **已确认关联进入复审**（海关更正或人工复审）时：占用金额**冻结不释放**，新的编译运行不再把它计入已证实到账量；
   只有 `void`（释放）或 `reassign`（转移）才按 `balance_holds` 审计记录变动余额。

### 其余规则

- **海关更正**：原行与更正行通过 `revises_id` 连接为修订链；已提交/已审核行转为 `correction_pending`，
  旧审核记录通过 `declaration_events` 仍可回查。更正**不会改写**受理账中已提交的原始金额。
  若更正后申报金额低于已冻结占用，余额上限保持为已占用金额（`available = 0`）并生成
  `occupancy_exceeds_corrected_amount` 差异；撤销/改配后上限回落到更正金额。
- **税务回执乱序**：以签发时间为主序、可验证序号为同序时的次序依据；乱序到达但签发更早的回执按签发位置生效。
  次序无法证明（签发时间相同且无可用序号）时进入**回执冲突**，不推进申报状态；若此前已据此转换状态，转换被撤回并留痕。
- **退运凭证**：比较凭证生效时间、运输时序与原行证据；接收时间不足以证明出口日不存在资格。
  无法唯一归属时保留为**反向数量候选**（`reverse_candidates`），不改变申报数量、金额和已证实到账量。
- **装箱单**：仅佐证数量，从不引入金额；数量与申报不符时生成 `packing_quantity_mismatch`。

---

## 4. 验证方式

### 4.1 自动化验收测试（25 项，含真实并发）

```bash
.venv/bin/python -m pytest tests -q
```

测试使用独立数据库 `ccdev_test`（自动创建，不影响演示库 `ccdev`）。覆盖：

| 场景 | 测试 |
| --- | --- |
| 合并流水无唯一键 → 只生成候选、金额留在待分摊 | `test_combined_remittance_without_reference_stays_unallocated` |
| 唯一附言快速关联 + 部分到账 | `test_unique_reference_quick_reconciliation_and_partial_receipt` |
| 并发确认不得超额（含两侧余额不变式） | `test_concurrent_confirmations_never_over_allocate`、`test_two_analysts_same_candidate_only_one_wins`、`test_concurrency.py`（真实 uvicorn + 线程并发） |
| 海关更正：修订链、旧审核可回查、原始申报依据不被改写 | `test_customs_correction_preserves_submitted_basis_and_revision_chain` |
| 更正后金额低于已占用：冻结而非改写、撤销后上限回落 | `test_customs_correction_below_committed_amount_freezes_instead_of_rewriting` |
| 乱序回执按签发时间生效 / 次序不可证 → 回执冲突 | `test_out_of_order_tax_receipts_ordered_by_issue_time`、`test_tax_receipt_sequence_breaks_an_issue_time_tie`、`test_tax_receipt_order_conflict_when_ordering_unprovable`、`test_conflicted_tax_receipt_does_not_advance_declaration` |
| 跨主体授权 | `test_cross_entity_authorization_is_enforced` |
| 退运候选不改变申报与到账事实 | `test_return_document_becomes_reverse_candidate_not_eligibility_change`、`test_return_evidence_that_cannot_be_attributed_stays_a_candidate` |
| 事实来源展开（来源文件/状态账/余额占用/分摊情景/修订链/判定规则） | `test_drilldown_contains_ledgers_balances_scenarios_revision_chain` |
| 已确认关联进入复审 → 新编译不计入已证实到账量、占用冻结 | `test_frozen_confirmed_line_excluded_from_facts` |
| 撤销释放占用并留审计记录 | `test_void_releases_occupancy_with_audit_records` |
| 装箱单只佐证数量 | `test_packing_list_corroborates_quantity_without_touching_amounts` |
| 出口行侧限额约束（两笔唯一关联合计不得超过申报额） | `test_export_line_side_bounds_cumulative_confirmation` |
| 撤销后释放金额可再次确认 | `test_void_then_pass_can_reconfirm_freed_amount` |
| 更正必须指向被更正行（不允许静默改写申报基准） | `test_correction_without_revises_pointer_is_rejected` |
| 单次运行中后一笔被阻塞不影响前一笔成功（SAVEPOINT） | `test_blocked_unique_link_does_not_undo_earlier_confirmations_in_the_same_pass` |
| 两状态账无共享已结清字段 | `test_settlement_ledgers_do_not_share_settled_fields` |

> 并发用例做过变异验证：临时移除余额行的 `FOR UPDATE` 行锁后，`test_concurrency.py` 立即失败
> （3 个并发确认全部返回 201，累计超出银行实际金额），恢复行锁后通过——说明该用例确实在检验原子性。

> `test_concurrency.py` 会自行拉起一个本地 uvicorn 进程（随机端口）并发发起确认请求，结束后关闭。

> 说明：本工作容器内没有可用的 `docker` 可执行文件，因此 `Dockerfile` / `docker-compose.yml` 未在此环境实际构建验证；
> 已验证的是本地 PostgreSQL 18 + venv 方式的完整启动、播种、接口与测试（见下）。

### 4.2 手工验证（seed 数据已就绪）

演示数据：主体 `ENT-A` 有 1 张发票 3 行（`SKU-AA-100` 1200 / `SKU-BB-200` 1800 / `SKU-CC-300` 2000，申报号 `DECL-2026-100`），
报关档案、税务回执（批次 `B-2026-09-01`）齐备，三行均已 `submitted → verified`；
银行到账两笔：`TXN-2026-10-A` 1000.00（附言含 `SKU-AA-100`）、`TXN-2026-10-B` 2500.00（合并付款，无行级键）。

```bash
K='X-API-Key: demo-analyst-1-key'
B=http://127.0.0.1:8000

# ① 两个状态账：申报状态与到账状态彼此独立
curl -s -H "$K" $B/api/v1/entities/ENT-A/status-ledgers

# ② 余额：银行侧 1000 已占用，出口行 1 已占用 1000 / 可用 200；合并流水 2500 全部可用
curl -s -H "$K" $B/api/v1/entities/ENT-A/balances

# ③ 候选：合并流水按约束生成 3 条候选（不自动分摊）
curl -s -H "$K" "$B/api/v1/entities/ENT-A/candidates?status=proposed"

# ④ 确认一条候选的部分金额（少于候选额），余额两侧同步占用
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"amount":"200.00","note":"line 1 残余"}' $B/api/v1/candidates/<candidate_id>/confirm

# ⑤ 超额尝试被拒（409）并留下差异原因
curl -s -X POST -H "$K" -H 'Content-Type: application/json' -d '{}' $B/api/v1/candidates/<candidate_id>/confirm

# ⑥ 编译事实：只有 active 对账行计入已证实事实，其余进入未分摊/差异
curl -s -X POST -H "$K" -H 'Content-Type: application/json' \
  -d '{"entity_code":"ENT-A"}' $B/api/v1/facts/compile

# ⑦ 任意对账行展开：来源文件、状态账、余额占用、分摊情景、修订链、判定规则
curl -s -H "$K" $B/api/v1/reconciliation-lines/<line_id>

# ⑧ 海关更正（改为 1150.00）→ correction_pending + 修订链 + 已确认行进入复审
curl -s -X POST -H "$K" -H 'Content-Type: application/json' -d '{
 "doc_type":"customs_correction","entity_code":"ENT-A","external_no":"DECL-2026-100-REV1",
 "original_location":"s3://demo/customs/DECL-2026-100-rev1.xml","received_time":"2026-10-10T08:00:00Z",
 "lines":[{"line_no":1,"sku":"SKU-AA-100","description":"Widget A","quantity":"98","amount":"1150.00",
           "currency":"USD","declaration_no":"DECL-2026-100","revises_line_no":1}]}' \
  $B/api/v1/intake/documents
curl -s -H "$K" $B/api/v1/export-lines/1        # 修订链 / 状态 / 余额 / 冻结行

# ⑨ 跨主体拒绝（403）
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-API-Key: demo-analyst-b-key' \
  $B/api/v1/entities/ENT-A/status-ledgers
```

---

## 5. API 一览（全部在 Swagger UI 中可直接调用）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/v1/admin/entities`、`POST /api/v1/admin/users`、`POST /api/v1/admin/users/{name}/grants` | 主体与账号管理（admin） |
| `POST /api/v1/intake/documents` | 受理任意来源文档（含行）；append-only |
| `GET /api/v1/intake/documents`、`GET /api/v1/intake/documents/{id}` | 查询来源文档、行与更正链 |
| `POST /api/v1/entities/{code}/resolve` | 运行行级身份解析器，生成/刷新候选案卷 |
| `GET /api/v1/entities/{code}/dossiers`、`GET /api/v1/dossiers/{id}` | 候选案卷与证据等级 |
| `POST /api/v1/entities/{code}/reconcile-pass` | 先确认唯一关联，再生成合并流水候选 |
| `GET /api/v1/entities/{code}/candidates` | 候选列表（含理由） |
| `POST /api/v1/candidates/{id}/confirm` | 确认候选（原子占用两侧余额，可部分确认） |
| `POST /api/v1/reconciliation-lines/{id}/void` / `/reassign` / `/review` | 撤销释放 / 改配转移 / 进入复审冻结 |
| `GET /api/v1/reconciliation-lines/{id}` | **对账行全展开**：来源文件、状态账与历史、余额占用与流水、分摊情景、修订链、判定规则 |
| `POST /api/v1/facts/compile`、`GET /api/v1/facts/latest`、`GET /api/v1/facts/runs/{id}` | 事实编译（只发布已证实事实、未分摊余额、差异原因） |
| `GET /api/v1/entities/{code}/balances` | 两侧余额 |
| `GET /api/v1/entities/{code}/status-ledgers` | 申报状态账、到账状态账、出口行到账概况 |
| `POST /api/v1/export-lines/{id}/declaration-events` | 申报状态事件（submitted / verified / rejected / draft_recorded） |
| `GET /api/v1/export-lines/{id}` | 出口行详情（状态、余额、修订链、对账行） |
| `GET /api/v1/entities/{code}/tax-receipts` / `/reverse-candidates` / `/packing-lines` / `/difference-cases` | 回执顺序、退运候选、装箱证据、差异原因 |

---

## 6. 目录结构

```
app/
  main.py                 FastAPI 应用、Swagger UI、OpenAPI
  config.py db.py        配置与数据库
  models.py              全部表模型（含 CHECK 约束与唯一约束）
  schemas.py             请求/响应模型
  auth.py                API Key + 主体授权
  services/
    intake.py            受理账写入、行解析、海关更正链、回执排序、退运候选
    resolver.py          行级身份解析器、带证据等级的案卷、受约束分摊候选
    ledgers.py           申报状态账 / 到账状态账（互不共享已结清字段）
    balance.py           余额控制器：唯一关联优先、原子占用、冻结/释放/转移
    facts.py             事实编译器与对账行全展开
  routers/               admin / intake / reconciliation
scripts/                 wait_for_db.py、seed.py
tests/                   conftest.py、helpers.py、test_acceptance.py、test_concurrency.py
```

---

## 7. 明确的非目标

- 不提交报关单或退税申报，不向海关/税务/银行发起任何写操作。
- 不操作库存数量、税务余额、银行资金，不生成付款、授信或税务处理指令。
- 不按金额比例、行数或任何启发式规则自动分摊合并回款；未确认金额始终保留为待分摊余额。
- 退运凭证不会自动冲减申报数量或已证实到账量。
