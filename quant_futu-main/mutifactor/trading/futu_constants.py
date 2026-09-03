"""
富途API常量定义
直接使用富途的枚举类型，避免映射转换
"""
from futu import OrderStatus, TrdSide, OrderType as FutuOrderType

__all__ = [
    'OrderStatus',
    'TrdSide',
    'FutuOrderType',
    # 订单状态名称（用于日志显示）
    'ORDER_STATUS_NAMES',
]

# 订单状态中文名称映射（仅用于日志显示）
ORDER_STATUS_NAMES = {
    OrderStatus.SUBMITTING: '提交中',
    OrderStatus.SUBMITTED: '已提交',
    OrderStatus.FILLED_ALL: '完全成交',
    OrderStatus.FILLED_PART: '部分成交',
    OrderStatus.CANCELLED_ALL: '全部取消',
    OrderStatus.CANCELLED_PART: '部分取消',
    OrderStatus.CANCELLING_ALL: '正在取消全部',
    OrderStatus.CANCELLING_PART: '正在取消部分',
    OrderStatus.FAILED: '失败',
    OrderStatus.DISABLED: '已禁用',
    OrderStatus.NONE: '无',
    OrderStatus.UNSUBMITTED: '未提交',
    OrderStatus.WAITING_SUBMIT: '等待提交',
    OrderStatus.SUBMIT_FAILED: '提交失败',
    OrderStatus.TIMEOUT: '超时',
    OrderStatus.DELETED: '已删除',
    OrderStatus.FILL_CANCELLED: '成交后取消',
}


def get_order_status_name(status: OrderStatus) -> str:
    """获取订单状态的中文名称"""
    if status is None:
        return 'None'
    return ORDER_STATUS_NAMES.get(status, f'未知状态({status})')
