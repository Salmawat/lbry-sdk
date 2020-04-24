from prometheus_client import Counter, Histogram, Gauge


class PrometheusMetrics:
    PENDING_REQUESTS: Gauge
    REQUESTS_COUNT: Counter
    RESPONSE_TIMES: Histogram

    __slots__ = [
        'PENDING_REQUESTS',
        'REQUESTS_COUNT',
        'RESPONSE_TIMES',
        '_installed',
        'namespace',
    ]

    def __init__(self):
        self._installed = False
        self.namespace = "daemon_api"

    def uninstall(self):
        self._installed = False
        for item in self.__slots__:
            current = getattr(self, item, None)
            if current:
                setattr(self, item, None)
                del current

    def install(self):
        if self._installed:
            return
        self._installed = True
        self.PENDING_REQUESTS = Gauge(
            "pending_requests", "Number of running api requests", namespace=self.namespace,
            labelnames=("method",)
        )
        self.REQUESTS_COUNT = Counter(
            "requests_count", "Number of requests received", namespace=self.namespace,
            labelnames=("method",)
        )
        self.RESPONSE_TIMES = Histogram(
            "response_time", "Response times", namespace=self.namespace,
            labelnames=("method",)
        )


METRICS = PrometheusMetrics()
