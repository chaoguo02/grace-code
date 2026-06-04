"""
tools/utils.py

跨工具共享的纯函数，无副作用，无依赖。
"""


def truncate_output(text: str, max_chars: int) -> str:
    """截断文本：保留头部 60% + 尾部 40%，中间显示省略信息。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n... [{omitted} characters truncated] ...\n"
        + text[-tail:]
    )
