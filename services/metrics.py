from collections import Counter


class MessageMetrics:
    """Small in-process metrics boundary, replaceable by Prometheus/OpenTelemetry."""

    def __init__(self):
        self._counts = Counter()

    def increment(self, name: str, **labels):
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        self._counts[key] += 1

    def snapshot(self):
        return dict(self._counts)
