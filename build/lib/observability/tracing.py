from __future__ import annotations

import contextlib
import contextvars
import logging
import random
from typing import Any

from config.schema import AppConfig, ObservabilityConfig
from observability.langfuse_client import create_langfuse_client
from observability.masking import sanitize_for_langfuse
from observability.models import build_task_input, build_task_metadata

logger = logging.getLogger(__name__)

_SUPPRESS_OBSERVABILITY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "forge_agent_observability_suppressed",
    default=False,
)


class ObservationHandle:
    def update(self, **kwargs: Any) -> None:
        return None

    def event(
        self,
        *,
        name: str,
        metadata: dict[str, Any] | None = None,
        input_data: Any = None,
        output_data: Any = None,
        level: str | None = None,
    ) -> None:
        return None

    def score(
        self,
        *,
        name: str,
        value: float | bool | str,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class _LangfuseObservationHandle(ObservationHandle):
    def __init__(self, client: Any, observation: Any, config: ObservabilityConfig) -> None:
        self._client = client
        self._observation = observation
        self._config = config

    def update(self, **kwargs: Any) -> None:
        if self._observation is None:
            return
        payload = {
            key: _sanitize_payload(value, self._config)
            for key, value in kwargs.items()
            if value is not None
        }
        if not payload:
            return
        try:
            self._observation.update(**payload)
        except TypeError:
            try:
                self._observation.update(payload)
            except Exception as exc:
                logger.warning("Langfuse observation update failed: %s", exc)
        except Exception as exc:
            logger.warning("Langfuse observation update failed: %s", exc)

    def event(
        self,
        *,
        name: str,
        metadata: dict[str, Any] | None = None,
        input_data: Any = None,
        output_data: Any = None,
        level: str | None = None,
    ) -> None:
        trace_id = self._first_attr("trace_id", "traceId")
        if trace_id is None:
            logger.debug("Skipping Langfuse event '%s': missing trace_id", name)
            return

        kwargs: dict[str, Any] = {
            "trace_id": trace_id,
            "name": name,
        }
        observation_id = self._first_attr("observation_id", "observationId", "id")
        if observation_id is not None:
            kwargs["parent_observation_id"] = observation_id
        if metadata:
            kwargs["metadata"] = _sanitize_payload(metadata, self._config)
        if input_data is not None:
            kwargs["input"] = _sanitize_payload(input_data, self._config)
        if output_data is not None:
            kwargs["output"] = _sanitize_payload(output_data, self._config)
        if level:
            kwargs["level"] = level

        for method_name in ("create_event", "create_trace_event"):
            method = getattr(self._client, method_name, None)
            if method is None:
                continue
            try:
                method(**kwargs)
                return
            except Exception as exc:
                logger.warning("Langfuse event write failed for '%s' via %s: %s", name, method_name, exc)
                return

    def score(
        self,
        *,
        name: str,
        value: float | bool | str,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        trace_id = self._first_attr("trace_id", "traceId")
        observation_id = self._first_attr("observation_id", "observationId", "id")
        if trace_id is None:
            logger.debug("Skipping Langfuse score '%s': missing trace_id", name)
            return

        kwargs: dict[str, Any] = {
            "trace_id": trace_id,
            "name": name,
            "value": value,
            "data_type": _infer_score_data_type(value),
        }
        if observation_id is not None:
            kwargs["observation_id"] = observation_id
        if comment:
            kwargs["comment"] = comment
        if metadata:
            kwargs["metadata"] = _sanitize_payload(metadata, self._config)

        try:
            self._client.create_score(**kwargs)
        except Exception as exc:
            logger.warning("Langfuse score write failed for '%s': %s", name, exc)

    def _first_attr(self, *names: str) -> Any | None:
        for name in names:
            value = getattr(self._observation, name, None)
            if value is not None:
                return value
        return None


class _NoOpContext:
    def __enter__(self) -> ObservationHandle:
        return ObservationHandle()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _SuppressedContext(_NoOpContext):
    def __init__(self) -> None:
        self._token: contextvars.Token[bool] | None = None

    def __enter__(self) -> ObservationHandle:
        self._token = _SUPPRESS_OBSERVABILITY.set(True)
        return ObservationHandle()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            _SUPPRESS_OBSERVABILITY.reset(self._token)
        return False


class _LangfuseContext:
    def __init__(
        self,
        *,
        client: Any,
        config: ObservabilityConfig,
        propagate_attributes: Any,
        name: str,
        as_type: str,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._propagate_attributes = propagate_attributes
        self._name = name
        self._as_type = as_type
        self._input_data = input_data
        self._metadata = metadata or {}
        self._model = model
        self._session_id = session_id
        self._user_id = user_id
        self._propagation_cm: Any | None = None
        self._observation_cm: Any | None = None
        self._handle = ObservationHandle()

    def __enter__(self) -> ObservationHandle:
        try:
            if self._session_id or self._user_id:
                propagate_kwargs = {}
                if self._session_id:
                    propagate_kwargs["session_id"] = self._session_id
                if self._user_id:
                    propagate_kwargs["user_id"] = self._user_id
                self._propagation_cm = self._propagate_attributes(**propagate_kwargs)
                self._propagation_cm.__enter__()

            kwargs: dict[str, Any] = {
                "as_type": self._as_type,
                "name": self._name,
            }
            sanitized_input = _sanitize_payload(self._input_data, self._config)
            sanitized_metadata = _sanitize_payload(self._metadata, self._config)
            if sanitized_input is not None:
                kwargs["input"] = sanitized_input
            if sanitized_metadata:
                kwargs["metadata"] = sanitized_metadata
            if self._model:
                kwargs["model"] = self._model

            self._observation_cm = self._client.start_as_current_observation(**kwargs)
            observation = self._observation_cm.__enter__()
            self._handle = _LangfuseObservationHandle(self._client, observation, self._config)
            return self._handle
        except Exception as exc:
            logger.warning("Failed to start Langfuse observation '%s': %s", self._name, exc)
            self._cleanup()
            return ObservationHandle()

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self._observation_cm is not None:
                self._observation_cm.__exit__(exc_type, exc, tb)
        except Exception as close_exc:
            logger.warning("Failed to close Langfuse observation '%s': %s", self._name, close_exc)
        finally:
            self._cleanup()
        return False

    def _cleanup(self) -> None:
        if self._propagation_cm is not None:
            with contextlib.suppress(Exception):
                self._propagation_cm.__exit__(None, None, None)
            self._propagation_cm = None
        self._observation_cm = None


class BaseObserver:
    @property
    def config(self) -> ObservabilityConfig | None:
        return None

    def start_task(self, task: Any) -> Any:
        return _NoOpContext()

    def start_generation(
        self,
        *,
        name: str,
        model: str,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        return _NoOpContext()

    def start_tool(
        self,
        *,
        name: str,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        return _NoOpContext()

    def flush(self) -> None:
        return None


class NoOpObserver(BaseObserver):
    pass


class LangfuseObserver(BaseObserver):
    def __init__(self, client: Any, config: ObservabilityConfig, propagate_attributes: Any) -> None:
        self._client = client
        self._config = config
        self._propagate_attributes = propagate_attributes

    @property
    def config(self) -> ObservabilityConfig | None:
        return self._config

    def start_task(self, task: Any) -> Any:
        if _SUPPRESS_OBSERVABILITY.get():
            return _NoOpContext()
        sample_rate = max(0.0, min(1.0, float(self._config.sample_rate)))
        if sample_rate <= 0.0 or random.random() > sample_rate:
            return _SuppressedContext()
        metadata = build_task_metadata(task)
        metadata["environment"] = self._config.environment
        return _LangfuseContext(
            client=self._client,
            config=self._config,
            propagate_attributes=self._propagate_attributes,
            name="grace-task",
            as_type="span",
            input_data=build_task_input(task),
            metadata=metadata,
            session_id=task.metadata.get("session_id"),
            user_id=task.metadata.get("user_id"),
        )

    def start_generation(
        self,
        *,
        name: str,
        model: str,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        if _SUPPRESS_OBSERVABILITY.get():
            return _NoOpContext()
        return _LangfuseContext(
            client=self._client,
            config=self._config,
            propagate_attributes=self._propagate_attributes,
            name=name,
            as_type="generation",
            input_data=input_data,
            metadata=metadata,
            model=model,
        )

    def start_tool(
        self,
        *,
        name: str,
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        if _SUPPRESS_OBSERVABILITY.get():
            return _NoOpContext()
        return _LangfuseContext(
            client=self._client,
            config=self._config,
            propagate_attributes=self._propagate_attributes,
            name=name,
            as_type="span",
            input_data=input_data,
            metadata=metadata,
        )

    def flush(self) -> None:
        if not self._config.flush_on_exit:
            return
        try:
            self._client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed: %s", exc)


def _sanitize_payload(value: Any, config: ObservabilityConfig) -> Any:
    return sanitize_for_langfuse(
        value,
        mask_sensitive_data=config.mask_sensitive_data,
    )


def _infer_score_data_type(value: float | bool | str) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, str):
        return "TEXT"
    return "NUMERIC"


_OBSERVER: BaseObserver = NoOpObserver()


def configure_observability(config: AppConfig) -> BaseObserver:
    global _OBSERVER
    client, propagate_attributes = create_langfuse_client(config.observability)
    if client is None or propagate_attributes is None:
        _OBSERVER = NoOpObserver()
    else:
        _OBSERVER = LangfuseObserver(client, config.observability, propagate_attributes)
    return _OBSERVER


def get_observer() -> BaseObserver:
    return _OBSERVER


def flush_observability() -> None:
    _OBSERVER.flush()
