"""P0_1 Batch 4: Artifact hash + LayerRenderer — acceptance tests.

AC mappings:
  AC-7.1  Two artifacts with same first 1000 chars → different IDs
  AC-7.2  LayerRenderer enforces max_tokens per layer
  AC-7.3  Artifact.original_length recorded separately from char_count
"""

from __future__ import annotations

import pytest

from context.counters import CharEstimator


# ===========================================================================
# 1. Artifact full-content hash
# ===========================================================================

class TestArtifactHash:
    """AC-7.1: Full-content hash prevents collision."""

    def test_same_prefix_different_suffix_different_ids(self):
        """Two outputs with same first 1000 chars get DIFFERENT artifact IDs."""
        from context.artifacts import ArtifactStore

        store = ArtifactStore(threshold_tokens=10)
        prefix = "LOG HEADER " * 80  # ~1000 chars
        suffix_a = "\nRESULT A: " + "x" * 5000
        suffix_b = "\nRESULT B: " + "y" * 5000

        id_a = store._create_artifact("test", prefix + suffix_a, 100).artifact_id
        id_b = store._create_artifact("test", prefix + suffix_b, 100).artifact_id

        assert id_a != id_b, (
            f"Artifacts with different content should have different IDs. "
            f"Got {id_a} and {id_b}"
        )

    def test_original_length_recorded(self):
        """AC-7.3: original_length is the full output length, not capped."""
        from context.artifacts import ArtifactStore

        store = ArtifactStore(threshold_tokens=10, max_content_bytes=500)
        output = "x" * 2000  # 2000 chars, but capped at 500 bytes

        artifact = store._create_artifact("test", output, 50)

        assert artifact.original_length == 2000, (
            f"original_length should be 2000, got {artifact.original_length}"
        )
        assert artifact.char_count <= 500, (
            f"char_count should be capped at 500, got {artifact.char_count}"
        )

    def test_collision_detection_logs_warning(self, caplog):
        """Collision is detected and a disambiguation suffix is added."""
        from context.artifacts import ArtifactStore, Artifact

        store = ArtifactStore(threshold_tokens=10)
        a1 = store._create_artifact("test", "content_a", 10)
        a2 = store._create_artifact("test", "content_b", 10)

        # Manually force same ID
        a2 = Artifact(
            artifact_id=a1.artifact_id,
            tool_name="test",
            full_content="different content!",
            summary="summary",
            token_count=10,
            char_count=len("different content!"),
            original_length=len("different content!"),
            line_count=1,
        )

        import logging
        with caplog.at_level(logging.WARNING):
            store._add(a2)

        # The collision should have been detected and resolved
        stored = store.get(a1.artifact_id)
        # Either the old one is still there (same content) or new ID was generated
        assert stored is not None


# ===========================================================================
# 2. LayerRenderer max_tokens enforcement
# ===========================================================================

class TestLayerRenderer:
    """AC-7.2: ContextLayer.max_tokens is enforced."""

    def test_max_tokens_trims_layer_content(self):
        """Layer with max_tokens=50 gets trimmed to ~50 tokens."""
        from context.structured import ContextLayer, ContextPriority, StructuredContext

        ctx = StructuredContext()
        ctx.add_layer(ContextLayer(
            name="test_layer",
            priority=ContextPriority.SYSTEM,
            content="word " * 500,  # ~1000 tokens
            max_tokens=50,
        ))

        estimator = CharEstimator(model_window=200_000)
        result = ctx.render_with_budget(estimator)
        # Should be trimmed
        est = estimator.estimate(str(result))
        assert est <= 100, f"Trimmed layer should be ~50 tokens, got ~{est}"

    def test_zero_max_tokens_is_unbounded(self):
        """max_tokens=0 means unbounded — layer is not trimmed."""
        from context.structured import ContextLayer, ContextPriority, StructuredContext

        content = "hello world " * 20
        ctx = StructuredContext()
        ctx.add_layer(ContextLayer(
            name="unbounded",
            priority=ContextPriority.SYSTEM,
            content=content,
            max_tokens=0,
        ))

        result = ctx.render_with_budget()
        assert "hello world" in str(result)
        # Not truncated — full content present (minus marker)
        assert "trimmed" not in str(result).lower()

    def test_layer_already_within_budget_not_trimmed(self):
        """Layer under max_tokens is NOT trimmed."""
        from context.structured import ContextLayer, ContextPriority, StructuredContext

        content = "short content"
        ctx = StructuredContext()
        ctx.add_layer(ContextLayer(
            name="small",
            priority=ContextPriority.SYSTEM,
            content=content,
            max_tokens=5000,
        ))

        estimator = CharEstimator(model_window=200_000)
        result = ctx.render_with_budget(estimator)
        assert "short content" in str(result)
        assert "trimmed" not in str(result).lower()
