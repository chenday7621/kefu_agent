"""On-demand deterministic receipts from owned database facts; no answer model."""

from decimal import Decimal
import re

RECOVERY_QUESTIONS = re.compile(
    r"(刚才|刚刚|之前|上次).*(退货|申请).*(成功|提交|状态|结果|怎么样)|^(查询|恢复)(原操作|退货操作|操作结果)|^退货(申请)?(成功了吗|提交了吗|状态)"
)
RETRY_QUESTIONS = re.compile(r"^(请)?按原操作重试[。！？!? ]*$")


def receipt(result):
    if not result["ok"]:
        return "暂时无法核实原退货操作，正在核实。原操作保持不变，不会生成新操作编号重复提交。"
    data = result["data"]
    request = data["request"]
    original = data["original_parameters"]
    scope = f"原操作：{data['operation_id']}\n订单：{original['order_id']}\n明细：{original['item_id']}\n数量：{original['quantity']}"
    if request:
        return f"退货申请已提交。\n{scope}\n申请编号：{request['id']}\n申请金额：{Decimal(request['amount_cents']) / 100:.2f} CNY\n状态：{request['status']}\n仅表示申请提交，尚未退款，也不表示审批或物流完成。"
    if data["state"] == "awaiting_confirmation":
        return (
            f"原操作尚未收到真实用户确认，尚未提交。\n{scope}\n请先核对原申请内容并提供原确认口令。"
        )
    if data["state"] == "superseded_not_submitted":
        return f"原操作尚未提交，其内容已被新准备内容替代，旧口令不能授权。\n{scope}\n请先核对并确认新操作。"
    if data["state"] == "expired_not_submitted":
        return (
            f"原操作尚未提交，已超过有效期，不能凭过期口令提交。\n{scope}\n不会自动生成新操作重提。"
        )
    return f"已保存真实用户确认，但目前未查到申请，尚不能声称提交成功。\n{scope}\n如需重试，请明确发送“按原操作重试”；只会复用原操作编号和原参数。"


def latest_result(business, session_id, operation_id=None):
    bound = business.session_operation(session_id, operation_id)
    return business.get_operation_result(bound["data"]["operation_id"]) if bound["ok"] else bound
