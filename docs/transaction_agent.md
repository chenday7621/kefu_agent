# Transaction Agent：冻结 O1-B

企业客服事务需要把对话意图转为正确的账户、订单和商品操作。工具已返回信息时，Agent 仍可能停止查找；商品检索返回多个变体时，也可能混淆硬约束、偏好及实际操作范围。

本项目在 Parlant 3.3.2 中桥接官方 Retail 工具，由官方环境执行工具并评分。BASE 为 B0-R1 的完整官方政策与原业务关联；O1-B 改进业务关联中的推理指引。没有模型训练，也没有另一套程序代替 Agent 做事务决策。

## Baseline 和 O1-A：账户查询

B0-R1 的开发集轨迹中，任务 24、83、85 已通过 `get_user_details` 获得订单 ID，却仍要求用户提供订单号，没有继续查询详情。O1-A 要求将这些 ID 作为订单查找证据，调用 `get_order_details` 查找与用户描述匹配的订单；不得假设订单列表已按日期排序。

代表案例 **train task 24**：B0-R1 没有继续查询订单详情；O1-A 查询四个订单后回答了 T-shirt 材质问题，官方 reward 从 0 变为 1。任务 85 虽然增加了详情查找，最终 reward 仍为 0，因此该优化没有解决所有失败。

## O1-B：商品约束与操作范围

O1-B 保留 O1-A 的账户查询规则，并增加两类推理指引：区分商品必须满足的属性与可放宽偏好；区分实际操作的原订单、原商品和用于比对的目标变体，范围含糊时先澄清。

- **train task 75，变体匹配**：O1-A 认为没有符合条件的耳机变体并转人工；O1-B 从工具结果中找到了可用的黑色、4 小时续航、非防水变体，完成确认与换货，DB reward 从 0 变为 1。
- **train task 14，操作范围**：O1-A 将 Action Camera 纳入笼统的 “gaming items” 退货范围；O1-B 澄清范围为 Mechanical Keyboard 与 Gaming Mouse，排除相机，DB reward 从 0 变为 1。

以上案例均来自开发阶段的官方 train 任务轨迹，用于说明规则变化；不冒充 HOLDOUT 案例，也不发布任务全文或原始审计。其他任务仍存在失败，例如偏好匹配改善后仍可能因付款信息无法获得而未完成任务。

## 最终结果

| Evaluation | B0-R1 BASE | O1-B | Change |
| --- | --- | --- | --- |
| 官方 Retail test，40 tasks × 2 repeats / arm | 30/80，37.50% | 47/80，58.75% | +21.25pp |

两轮 seed 为 42 和 43，两组共 160 个已评分执行单元。两组共用 O1-B 的桥接、清理与观测路径，以比较业务关联配置。成绩来自自定义 DeepSeek 环境，不是官方默认榜单；开发集的 50% / 63.3% / 70% 不混入本表。

运行曾遇到余额中断和一次空回复基础设施故障。任务 `OPT_r2_t101` 使用了用户明确授权的恢复预算例外；全部轨迹在本地审计中保留。已完成的正常失败没有重跑。分数还受模型、模拟用户和评分随机性影响，不能将全部增益单独归因于某条规则，也不作统计显著性结论。

## 默认版本与源码

默认展示为 [O1-B policy](../benchmarks/parlant_retail_o1b/policy_config.py)。正式对照配置位于 [BASE](../benchmarks/parlant_retail_heldout/configs/BASE/policy_config.py) 与 [OPT](../benchmarks/parlant_retail_heldout/configs/OPT/policy_config.py)，调度入口为 [supervise.py](../benchmarks/parlant_retail_heldout/supervise.py)。C1 批量降本属于独立实验记录，不进入展示默认配置。数据与运行前置条件见 [复现说明](reproduce.md)。
