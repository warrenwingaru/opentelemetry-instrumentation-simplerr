from simplerr import Response
from werkzeug.test import Client
from opentelemetry.test.wsgitestutil import WsgiTestBase

from opentelemetry.instrumentation.simplerr import SimplerrInstrumentor
from .base_test import InstrumentationTest


class TestSQLCommenter(InstrumentationTest, WsgiTestBase):
    def setUp(self):
        super().setUp()
        SimplerrInstrumentor().instrument()
        self._create_app()
        self._common_initialization()

    def tearDown(self):
        super().tearDown()
        with self.disable_logging():
            SimplerrInstrumentor().uninstrument()

    def test_sqlcommenter_enabled_default(self):
        client = Client(self.app, Response)
        resp = client.get("/sqlcommenter")
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(
            list(resp.response)[0].strip(),
            b'{"framework": "simplerr:(.*)", "controller": (.*), "route": "/sqlcommenter"}'
        )

    def test_sqlcommenter_enabled_with_configurations(self):
        SimplerrInstrumentor().uninstrument()
        SimplerrInstrumentor().instrument(
            enable_commenter=True,
            commenter_options={"route": False}
        )

        self._create_app()
        client = Client(self.app, Response)
        resp = client.get("/sqlcommenter")
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(
            list(resp.response)[0].strip(),
            b'{"framework": "simplerr:(.*)", "controller": (.*)}'
        )

    def test_sqlcommenter_disabled(self):
        SimplerrInstrumentor().uninstrument()
        SimplerrInstrumentor().instrument(enable_commenter=False)

        self._create_app()
        client = Client(self.app, Response)
        resp = client.get("/sqlcommenter")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.response)[0].strip(), b'{}')
