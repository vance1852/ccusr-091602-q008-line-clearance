# 换线清场与首件放行

包装产线换产品（如含致敏原产品切普通产品）时，本系统记录清场证据、设备校验和首件结果，
让远程值守的质量员能确认是否具备开线条件。

## 规则要点

- **检查范围按风险生成**：`data/transition_matrix.json` 按"原产品 × 新产品"给出风险等级和
  必检范围（区域 × 检查类型），矩阵按版本管理；清场结论以**结论当时**的矩阵版本为准，
  升版后新增必检项不补齐不得结论。
- **只追加**：证据、机器回执、人工签字全部只追加（`app/store.py`）。离线终端消息带
  `(device_id, device_seq)` 设备内序号：重发幂等，同序号不同内容拒绝，
  **补传不得覆盖后来完成的检查**（留痕拒绝，项结果不变）。
- **逐项核对**：操作员逐项扫码（旧标签、清洗记录）、上传计量结果（余料、擦拭、校准）
  或声明异常（项以"不适用"结案）。旧标签重复扫描、扫到新产品标签一律拒绝。
- **残留与重新清洁**：发现残留（计量超限或人工巡检上报）触发重新清洁，
  只作废**相关区域**的检查项与签字并开启新一轮，不连带无关区域。
- **开线令牌**：清场通过 + 模具与程序校验一致 + 首件检测获批，三者齐备才签发；
  令牌始终绑定产线、产品、配方版本和有效期，过期/绑定不符/重复使用一律拒绝。
- **岗位校验**：区域清场签字、首件批准、模具程序校验、重新清洁确认均按
  `data/personnel.json` 的岗位策略校验，跨岗代签明确拒绝。

## 目录结构

```
app/
  models.py      状态机与领域对象（取值与 domain_contract.json 一致）
  catalog.py     主数据：区域、产品、版本化转换矩阵、人员岗位
  store.py       只追加事件日志
  service.py     核心服务：范围生成/证据/签字/重新清洁/结论/令牌
  batch_page.py  批次页面：从事件日志还原全部历史
data/
  zones.json                     区域清单（必须核对的对象）
  products.json                  产品（致敏原、模具/程序/配方要求、首件规格）
  transition_matrix.json         版本化产品转换矩阵（风险 → 检查范围）
  personnel.json                 人员岗位与签字策略
  equipment_receipts.sample.json 设备回执样例
tests/                           规则单测 + 高风险换线验收时间线
```

## 运行检查

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_acceptance_timeline.py` 是验收用例：一轮含致敏原 → 普通产品的高风险换线，
覆盖离线补传、旧标签重复扫描、跨岗代签、残留再清洁与重签、矩阵升版、首件获批、
令牌签发与核销；最后从批次页面还原每个区域的证据、失效重签原因、首件数据和令牌去向。

## 用法示例

```python
from app import Catalog, ChangeoverService, build_batch_page

catalog = Catalog.load("data")
service = ChangeoverService(catalog)

co = service.open_changeover("L1", "P-ALLERGEN", "P-PLAIN", opened_by="U-QA")
service.begin_checks(co)
# ... 逐项 submit_evidence / sign_zone / conclude_clearance /
#     verify_mold_program / submit_first_article / approve_first_article ...
token_id = service.issue_token(co)          # 三道闸门齐备才签发
service.consume_token(token_id, "L1", "P-PLAIN", "R-200")

page = build_batch_page(service.store, co)  # 批次页面: 区域证据/失效重签/首件/令牌
```
