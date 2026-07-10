# agent/__init__.py
#
# 延迟导入以避免循环依赖：
#   tools.base → agent.task → agent (触发 __init__.py)
#   → agent.core → tools.base (循环!)
#
# 所以 __init__.py 不做顶层 import，需要符号时从子模块直接引入。

__all__ = [
    "Agent", "AgentConfig", "ReActAgent",
    "create_agent",
]


def __getattr__(name: str):
    """延迟导入 __all__ 中的符号。"""
    if name in ("Agent", "AgentConfig", "ReActAgent"):
        from agent.core import Agent, AgentConfig, ReActAgent
        _mod = {"Agent": Agent, "AgentConfig": AgentConfig,
                "ReActAgent": ReActAgent}
        if name in _mod:
            globals()[name] = _mod[name]
            return _mod[name]
    if name == "create_agent":
        from agent.factory import create_agent
        globals()["create_agent"] = create_agent
        return create_agent
    raise AttributeError(f"module 'agent' has no attribute {name!r}")
