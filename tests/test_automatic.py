import os

from werkzeug.test import Client
from simplerr import Response

import simplerr.dispatcher
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.simplerr import SimplerrInstrumentor
from opentelemetry.test.wsgitestutil import WsgiTestBase
from .base_test import InstrumentationTest

class TestAutomatic(InstrumentationTest, WsgiTestBase):
    def setUp(self):
        super().setUp()

        SimplerrInstrumentor().instrument()

        self._create_app()

        self._common_initialization()


    def tearDown(self):
        super().tearDown()
        with self.disable_logging():
            SimplerrInstrumentor().uninstrument()

    def test_uninstrument(self):
        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        SimplerrInstrumentor().uninstrument()
        self._create_app()
        self.client = Client(self.app, Response)

        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

    def test_excluded_urls_explicit(self):
        SimplerrInstrumentor().uninstrument()
        SimplerrInstrumentor().instrument(excluded_urls="/hello/456")

        self._create_app()
        self.client = Client(self.app, Response)

        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        resp = self.client.get("/hello/456")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 456"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

    def test_no_op_tracer_provider(self):
        SimplerrInstrumentor().uninstrument()

        SimplerrInstrumentor().instrument(tracer_provider=trace_api.NoOpTracerProvider())

        self._create_app()
        self.client = Client(self.app, Response)

        self.client.get("/hello/123")

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 0)




