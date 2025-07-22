import trace
from timeit import default_timer
from unittest.mock import patch, Mock

import simplerr.dispatcher
from opentelemetry.instrumentation.propagators import get_global_response_propagator, set_global_response_propagator, \
    TraceResponsePropagator
from opentelemetry.sdk.metrics._internal.point import HistogramDataPoint, NumberDataPoint
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.util.http import get_excluded_urls, OTEL_PYTHON_INSTRUMENTATION_HTTP_CAPTURE_ALL_METHODS
from werkzeug import Response

from opentelemetry import trace
from opentelemetry.instrumentation.simplerr import SimplerrInstrumentor
from tests.base_test import InstrumentationTest
from opentelemetry.test.wsgitestutil import WsgiTestBase
from opentelemetry.instrumentation.wsgi import (
    OpenTelemetryMiddleware,
    _active_requests_count_attrs,
    _duration_attrs
)


def expected_attributes(override_attributes):
    default_attributes = {
        SpanAttributes.HTTP_METHOD: "GET",
        SpanAttributes.HTTP_SERVER_NAME: "localhost",
        SpanAttributes.HTTP_SCHEME: "http",
        SpanAttributes.NET_HOST_PORT: 80,
        SpanAttributes.HTTP_HOST: "localhost",
        SpanAttributes.HTTP_TARGET: "/",
        SpanAttributes.HTTP_FLAVOR: "1.1",
        SpanAttributes.HTTP_STATUS_CODE: 200,
    }
    for k, v in override_attributes.items():
        default_attributes[k] = v
    return default_attributes


_expected_metric_names = [
    "http.server.duration",
    "http.server.active_requests",
]

_recommended_attrs = {
    "http.server.active_requests": [*_active_requests_count_attrs, "http.route"],
    "http.server.duration": [*_duration_attrs, "http.route"],
}


class TestProgrammatic(InstrumentationTest, WsgiTestBase):
    def setUp(self):
        super().setUp()

        self.env_patch = patch.dict(
            "os.environ",
            {
                "OTEL_PYTHON_SIMPLERR_EXCLUDED_URLS": "http://localhost/env_excluded_arg/123,env_excluded_noarg"
            }
        )
        self.env_patch.start()
        self.exclude_patch = patch(
            "opentelemetry.instrumentation.simplerr._excluded_urls_from_env",
            get_excluded_urls("SIMPLERR")
        )
        self.exclude_patch.start()

        self._create_app()
        SimplerrInstrumentor().instrument_app(self.app)

        self._common_initialization()

    def tearDown(self):
        super().tearDown()
        self.env_patch.stop()
        self.exclude_patch.stop()
        with self.disable_logging():
            SimplerrInstrumentor().uninstrument_app(self.app)

    def test_instrument_app_and_instrument(self):
        SimplerrInstrumentor().instrument()
        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        SimplerrInstrumentor().uninstrument()

    def test_uninstrument_app(self):
        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        SimplerrInstrumentor().uninstrument_app(self.app)

        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

    def test_uninstrument_app_and_instrument(self):
        SimplerrInstrumentor().instrument()
        SimplerrInstrumentor().uninstrument_app(self.app)
        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 0)
        SimplerrInstrumentor().uninstrument()

    def test_simple(self):
        expected_attrs = expected_attributes({
            SpanAttributes.HTTP_TARGET: "/hello/123",
            SpanAttributes.HTTP_ROUTE: "/hello/<int:helloid>",
        })

        self.client.get("/hello/123")

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        span = span_list[0]
        self.assertEqual(span.name, "GET /hello/<int:helloid>")
        self.assertEqual(span.kind, trace.SpanKind.SERVER)
        self.assertEqual(span.attributes, expected_attrs)

    def test_trace_response(self):
        orig = get_global_response_propagator()

        set_global_response_propagator(TraceResponsePropagator())
        resp = self.client.get("/hello/123")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        self.assertTraceResponseHeaderMatchesSpan(
            resp.headers,
            span_list[0]
        )

        set_global_response_propagator(orig)

    def test_not_recording(self):
        mock_tracer = Mock()
        mock_span = Mock()
        mock_span.is_recording.return_value = False
        mock_tracer.start_span.return_value = mock_span
        with patch("opentelemetry.trace.get_tracer") as tracer:
            tracer.return_value = mock_tracer
            self.client.get("/hello/123")
            self.assertFalse(mock_span.is_recording())
            self.assertTrue(mock_span.is_recording.called)
            self.assertFalse(mock_span.set_attribute.called)
            self.assertFalse(mock_span.set_status.called)

    def test_404(self):
        expected_attrs = expected_attributes({
            SpanAttributes.HTTP_METHOD: "POST",
            SpanAttributes.HTTP_TARGET: "/bye",
            SpanAttributes.HTTP_STATUS_CODE: 404
        })

        resp = self.client.post("/bye")
        self.assertEqual(404, resp.status_code)
        resp.close()
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        span = span_list[0]
        self.assertEqual(span.name, "POST /bye")
        self.assertEqual(span.kind, trace.SpanKind.SERVER)
        self.assertEqual(span.attributes, expected_attrs)

    def test_internal_error(self):
        expected_attrs = expected_attributes({
            SpanAttributes.HTTP_TARGET: "/hello/500",
            SpanAttributes.HTTP_STATUS_CODE: 500,
            SpanAttributes.HTTP_ROUTE: "/hello/<int:helloid>",
        })

        resp = self.client.get("/hello/500")
        self.assertEqual(500, resp.status_code)
        resp.close()
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        span = span_list[0]
        self.assertEqual(span.name, "GET /hello/<int:helloid>")
        self.assertEqual(span.kind, trace.SpanKind.SERVER)
        self.assertEqual(span.attributes, expected_attrs)

    def test_exclude_lists_from_env(self):
        self.client.get("/env_excluded_arg/123")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 0)

        self.client.get("/env_excluded_arg/125")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        self.client.get("/env_excluded_noarg")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        self.client.get("/env_excluded_noarg2")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

    def test_simplerr_metrics(self):
        start = default_timer()
        self.client.get("/hello/123")
        self.client.get("/hello/321")
        self.client.get("/hello/756")
        duration = max(round((default_timer() - start) * 1000), 0)
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        number_data_point_seen = False
        histogram_data_point_seen = False
        self.assertTrue(len(metrics_list.resource_metrics) != 0)
        for resource_metric in metrics_list.resource_metrics:
            self.assertTrue(len(resource_metric.scope_metrics) != 0)
            for scope_metric in resource_metric.scope_metrics:
                self.assertTrue(len(scope_metric.metrics) != 0)
                for metric in scope_metric.metrics:
                    self.assertIn(metric.name, _expected_metric_names)
                    data_points = list(metric.data.data_points)
                    self.assertEqual(len(data_points), 1)
                    for point in data_points:
                        if isinstance(point, HistogramDataPoint):
                            self.assertEqual(point.count, 3)
                            self.assertAlmostEqual(
                                duration, point.sum, delta=10
                            )
                            histogram_data_point_seen = True
                        if isinstance(point, NumberDataPoint):
                            number_data_point_seen = True
                        for attr in point.attributes:
                            self.assertIn(
                                attr, _recommended_attrs[metric.name]
                            )
        self.assertTrue(number_data_point_seen and histogram_data_point_seen)

    def test_simplerr_metric_values(self):
        start = default_timer()
        self.client.get("/hello/123")
        self.client.get("/hello/321")
        self.client.get("/hello/756")
        duration = max(round((default_timer() - start) * 1000), 0)
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for resource_metric in metrics_list.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    for point in list(metric.data.data_points):
                        if isinstance(point, HistogramDataPoint):
                            self.assertEqual(point.count, 3)
                            self.assertAlmostEqual(
                                duration, point.sum, delta=10
                            )
                        if isinstance(point, NumberDataPoint):
                            self.assertEqual(point.value, 0)

    def _assert_basic_metric(self, expected_duration_attr, expected_requests_count_attr):
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for resource_metric in metrics_list.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    for point in list(metric.data.data_points):
                        if isinstance(point, HistogramDataPoint):
                            self.assertDictEqual(
                                expected_duration_attr,
                                dict(point.attributes)
                            )
                        elif isinstance(point, NumberDataPoint):
                            self.assertDictEqual(
                                expected_requests_count_attr,
                                dict(point.attributes)
                            )
                            self.assertEqual(point.value, 0)

    def test_basic_metric_success(self):
        self.client.get("/hello/756")
        expected_duration_attr = {
            "http.method": "GET",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
            "net.host.port": 80,
            "http.status_code": 200,
            "http.route": "/hello/<int:helloid>",
        }
        expected_requests_count_attr = {
            "http.method": "GET",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
        }

        self._assert_basic_metric(expected_duration_attr, expected_requests_count_attr)

    def test_basic_metric_nonstandard_http_method_success(self):
        self.client.open("/hello/756", method="NONSTANDARD")
        expected_duration_attr = {
            "http.method": "UNKNOWN",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
            "net.host.port": 80,
            "http.status_code": 405,
        }
        expected_requests_count_attr = {
            "http.method": "UNKNOWN",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
        }
        self._assert_basic_metric(expected_duration_attr, expected_requests_count_attr)

    @patch.dict(
        "os.environ",
        {
            OTEL_PYTHON_INSTRUMENTATION_HTTP_CAPTURE_ALL_METHODS: "1"
        }
    )
    def test_basic_metric_nonstandard_http_method_allowed_success(self):
        self.client.open("/hello/756", method="NONSTANDARD")
        expected_duration_attr = {
            "http.method": "NONSTANDARD",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
            "net.host.port": 80,
            "http.status_code": 405,
        }
        expected_requests_count_attr = {
            "http.method": "NONSTANDARD",
            "http.host": "localhost",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "localhost",
        }
        self._assert_basic_metric(expected_duration_attr, expected_requests_count_attr)

    def test_metric_uninstrument(self):
        self.client.delete("/hello/756")
        SimplerrInstrumentor().uninstrument_app(self.app)
        self.client.delete("/hello/756")
        metrics_list = self.memory_metrics_reader.get_metrics_data()
        for resource_metric in metrics_list.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    for point in list(metric.data.data_points):
                        if isinstance(point, HistogramDataPoint):
                            self.assertEqual(point.count, 1)