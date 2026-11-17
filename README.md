# 观测管理后端（Observation Administration Backend）

纯 Python 标准库 + SQLite 实现的观测管理后端：标定 / 质控 / 观测的资料维护、资格复核、凭证批准、发布集查询与版本历史。无第三方依赖，Python ≥ 3.8。

## 1. 运行

```bash
python3 -m unittest discover -s tests -v      # 全部验收测试（含跨进程并发）
python3 accept.py                             # 独立验收：仅走公开 API 的 32 项端到端检查
python3 demo.py                               # 端到端演示（内存库）
```

命令行（数据库为 SQLite 文件，重启后保留全部版本）：

```bash
python3 -m observation_admin --db admin.sqlite3 apply   batch.json
python3 -m observation_admin --db admin.sqlite3 query   [--version N]
python3 -m observation_admin --db admin.sqlite3 approve OBS_ID CREDENTIAL
python3 -m observation_admin --db admin.sqlite3 proof   OBS_ID
python3 -m observation_admin --db admin.sqlite3 versions
python3 -m observation_admin --db admin.sqlite3 revisions [--kind KIND] [--id ID]
```

输出均为单行 JSON。被拒绝的请求在 stderr 输出 `{"error": CODE, "detail": ...}`，退出码 3，且**不改变任何状态**。

也可直接用 Python API：

```python
from observation_admin import Store, AdminError
with Store("admin.sqlite3") as store:
    store.apply({...})            # 资料维护（一次一个批次）
    store.query()                 # 当前版本的完整查询结果
    store.query(3)                # 历史版本 3 的完整查询结果（原样重放）
    store.approve("obs-1", cred)  # 用当前有效凭证批准
    store.proof("obs-1")          # 观测的全部依据与凭证
```

## 2. 数据模型

所有数值（读数、参考值、容差、增益、偏移）都是**有限十进制数**，以规范十进制字符串保存并做**精确**运算，不使用浮点，也不受 `decimal` 上下文精度影响（内部以 `fractions.Fraction` 计算）。输入可以是十进制字符串、整数或 `Decimal`；**拒绝二进制浮点数**（`0.1` 在二进制里并不等于十分之一，接受它就无法保证"精确"）。

| 记录 | 字段 |
| --- | --- |
| 标定 calibration | `id`, `instrument`, `start`, `end`, `gain`, `offset` |
| 质控 qc | `id`, `instrument`, `instant`, `reading`, `reference`, `tolerance` |
| 观测 observation | `id`, `instrument`, `instant`, `reading` |

- `id` 在同类记录内唯一、非空；`instrument` 非空。不同类之间可以同名。
- `instant` / `start` / `end` 是 UTC 整数秒；布尔、浮点、字符串均拒绝。
- 标定区间 `[start, end)`，`start < end`；同仪器区间**不重叠**，可有缺口，可首尾相接。
- 容差 `tolerance ≥ 0`，非负。
- **质控时刻全局唯一**（规则原文未限定仪器，故按全局解释）。观测不得与任何质控同刻。

质控记录含自己的**原始读数** `reading`：质控判定即"以该时刻标定换算 `reading`，与 `reference` 的偏差不超过 `tolerance` 才通过"（规则中"质控与参考值偏差"的换算对象）。

## 3. 业务语义

### 3.1 资格复核

对观测 `o`：

1. 用 `o` 自身时刻所在的标定换算：`gain × reading + offset`（精确）。无标定 → 阻断原因 `missing_calibration`（scope=observation）。
2. 取**同仪器**、严格早于 / 晚于 `o` 时刻的最近两条质控（不要求它们在某个区间内，最近的即可）。
   - 某一侧没有 → 阻断原因 `missing_neighbor`（该侧），**不再**为这一侧报"质控不通过"。
   - 某一侧质控时刻无标定覆盖 → 阻断原因 `missing_calibration`（scope=quality_control，注明侧）；这**不能跳过**。
   - 某一侧质控不通过 → 阻断原因 `qc_failed`（注侧、偏差、容差）；不能跳过该失败去用更远的质控。
3. 全部通过时才算**合格**，生成**凭证**。

阻断原因会全部给出（`reasons` 数组），而不是只给第一个。

> 注：`早于/晚于` 用严格不等号，所以"同刻"永远不会成为邻侧；观测与质控同刻在维护阶段已被拒绝。

### 3.2 凭证与批准

合格观测的**证据包**（evidence）包含完整内容与**修订身份**：观测本身、两侧质控、以及两侧质控与观测换算各自使用的标定（`revision` 为各自的修订号）；凭证 `credential` 是该证据包规范 JSON 的 SHA-256，并掺入该观测的**基础纪元** `basis_epoch`（见下）。

批准 = 提交 `(观测标识, 当前凭证)`：

- 凭证不是该观测此刻的凭证 → `invalid_credential`，不改变状态。
- 观测此刻不合格 → `ineligible`，不改变状态。
- 成功则写一条批准记录（新版本）；**同一有效凭证可重复批准**，每次都是新版本。返回的 `approved_version` 是批准成立时的版本号（即新版本 - 1），`version` 是本次提交后的版本。

### 3.3 失效（永不复活）

批准只在"其凭证仍是该观测此刻的凭证"时有效。凭证会因下列原因失效：

- 观测本身被整体替换（即使内容完全相同）→ 新修订身份 → 凭证不同。
- 该观测的两侧最近质控被替换（即使数值不变）。
- 两侧质控或观测所依赖的标定被替换（即使增益/偏移不变）。
- **任一批次使该观测前后两侧邻接身份改变**（例如在观测与某侧质控之间新增/搬入一条质控，或把原邻侧质控搬走）。
- 与之无关的记录修订**不影响**。

为覆盖"邻接恢复也不复活"，每个观测维护一个**单调递增的基础纪元**：只要该观测的证据依赖集合的签名（观测修订、两侧质控修订及其标定修订、是否通过、阻断原因）发生变化，纪元 +1，**永不回退**。凭证掺入纪元，所以：

- 数值/邻接被改回去时，签名虽回到旧形状，纪元已更高，旧凭证不再匹配，**不会复活**。
- 审批记录永不删除，只标记为失效（`approval.status = "expired"`），历史可查。`approval` 字段给出 `status`（`none` / `expired` / `valid`）、全部 `approval_ids` 与 `valid_approval_ids`。

失效后必须**重新取证批准**：重新查询得到新凭证再批准。旧凭证即使当时曾被批准，也永远不再被接受。

### 3.4 发布

当前版本的发布集 `released` 恰含：合格 **且** 批准有效的观测，附 `converted_value`、完整依据（观测、两侧质控、各自标定）与凭证。未发布的观测在 `observations` 中列出全部实际阻断原因：`missing_calibration`（区分观测自身 / 质控侧）、`missing_neighbor`（注侧）、`qc_failed`（注侧）、`unapproved` / `approval_expired`。

## 4. 资料维护

`apply(batch)`，`batch` 是 `{"calibration": [...], "qc": [...], "observation": [...]}`：

- 可为空子列表，但整批不可为空；
- 批内同类标识不得重复；
- **按整批最终状态校验**（例如同批新增的观测与质控在最终状态里同刻即拒绝）；
- 逐条新增或按标识整体替换，**没有删除**；
- 相同内容重新提交也产生**新修订身份**（新 `revision`，`prev_revision` 指向旧修订）；
- 任一条非法 → 整批拒绝，状态不变。

## 5. 版本、原子性与并发

- 初始资料、批准、发布均为空，版本号为 `0`。每次成功的维护或批准推进一个版本；`query()` / `query(N)` 均返回**该版本提交时冻结的完整查询结果**，因此历史发布可原样重现，初始版本 (0) 也总可读。
- 每次变更在一个 `BEGIN IMMEDIATE` 事务内完成"读取 → 校验 → 写记录/修订台账/批准/基础纪元 → 追加版本快照 → COMMIT"；失败或崩溃只留旧版本。文件库用 WAL + `synchronous=FULL`，重启保留全部已提交版本。
- 读事务 `BEGIN DEFERRED` 在首条语句取快照；SQLite 单写者、写提交等待读者，配合进程内可重入锁，**并发串行等价**，每次查询只看到某一个完整版本。跨进程由 SQLite 锁与 `busy_timeout` 保证。
- 未知版本、非法输入、失效凭证、冲突一律拒绝且不改状态。

## 6. 错误码

| code | 含义 |
| --- | --- |
| `invalid_input` | 字段缺失/多余、类型错误、空标识、非法小数、区间非正、负容差等 |
| `conflict` | 标定重叠、质控时刻重复、观测与质控同刻 |
| `invalid_credential` | 凭证不是该观测此刻的凭证 |
| `ineligible` | 观测此刻不合格，不能批准 |
| `not_found` | 观测不存在 |
| `unknown_version` | 版本不存在或类型非法 |
| `busy` / `closed` / `unavailable` / `corrupt_database` / `incompatible_database` | 运行环境错误 |

## 7. 设计取舍（规则未明说之处）

- **质控时刻全局唯一**：原文"质控时刻唯一"无仪器限定，按全局执行。
- **质控可以落在标定缺口里**：维护阶段只校验质控自身字段、时刻唯一性和与观测不同刻，不要求质控时刻有标定覆盖——标定缺口是明确允许的，且校准资料随时可能被整体替换而使原有覆盖消失。这样的质控在资格复核时被识别为"缺标定"（`missing_calibration`，scope=quality_control，注明侧与质控标识），绝不当作跳过或通过。`qc` 列表查询里其 `status` 为 `missing_calibration`、`passed` 为 `null`。
- **"缺邻侧不另报质控失败"**：只针对缺失的那一侧；另一侧若存在且失败，仍如实报告。
- **批准只在观测合格时才附着阻断原因**：依据本身已坏时报告的是依据问题（`missing_calibration`、`missing_neighbor`、`qc_failed` 等），不会再额外报 `unapproved` / `approval_expired`（此时批准本身也不可能成立）。
- **`approval` 状态三态**：`none`（从未批准）、`expired`（曾批准但已失效）、`valid`（当前有效）。
- **浮点一律拒绝**：命令行读取 JSON 时用 `parse_float=str` 把 JSON 小数精确转成字符串，所以批文件里写 `2.5` 也保持精确十进制。
- **输入规模上限**（纯属防御，非业务规则）：标识 ≤ 512 字符、单个数值 ≤ 500 位有效数字、指数绝对值 ≤ 1000。`exact.py` 顶部可调。

## 8. 文件

```
observation_admin/
  __init__.py     公开 API：Store, AdminError
  errors.py       统一异常
  exact.py        精确十进制（字符串规范化 + Fraction 运算）
  model.py        记录解析与字段校验
  rules.py        资格复核、阻断原因、证据包、凭证、基础签名
  store.py        SQLite 存储、事务、修订台账、批准、发布、版本快照
  cli.py          命令行
tests/
  test_admin.py   规则/校验/版本/持久化/线程并发
  test_process.py 多进程并发（同一数据库文件）
accept.py         独立验收脚本（32 项）
demo.py           端到端演示
```
