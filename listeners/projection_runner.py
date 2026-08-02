"""P15: Projection Runner — manages multiple projections with delivery receipt tracking."""

class ProjectionRunner:
    def __init__(self, projections: list) -> None:
        self._projections = projections

    def handle(self, envelope) -> list:
        return [p.on_event(envelope) for p in self._projections]
