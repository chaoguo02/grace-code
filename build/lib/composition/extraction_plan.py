"""
P20-P22: Extraction targets — modules to move from Runtime to dedicated layers.

These are documentation markers for the migration.  Actual extraction
happens in P21-P22 phases.
"""

# P20: Multi-Agent cutover
#   Current: agent/session/delegation_scheduler.py
#   Target:  application/coordinators/multi_agent_coordinator.py
#   Command: ExecuteDelegation, AggregateDelegation

# P21: Context/Message extraction from Runtime
#   Current: agent/session/runtime.py (build_runtime_messages)
#   Target:  listeners/context_assembler.py
#   Pattern: Runtime publishes RunStarted → ContextAssembler builds messages

# P22: Worktree/Evidence extraction
#   Current: agent/session/runtime.py (worktree worker)
#   Target:  application/coordinators/worktree_coordinator.py
#   Current: agent/session/runtime.py (evidence projection)
#   Target:  listeners/evidence_projection.py
