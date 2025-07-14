import os

import simplerr.dispatcher
from werkzeug.test import Client
from simplerr import Response

class InstrumentationTest:
    @staticmethod
    def _hello_endpoint(request, helloid):
        if helloid == 500:
            raise ValueError(":-(")
        return f"Hello: {helloid}"

    @staticmethod
    def _custom_response_headers():
        resp = Response("test response")
        resp.headers['content-type'] = 'text/plain; charset=utf-8'
        resp.headers['content-length'] = '12'
        resp.headers['custom-header'] = 'my-custom-value-1,my-custom-value-2'
        return resp

    def _create_app(self):
        self.app = simplerr.dispatcher.wsgi('tests/website')

    def _common_initialization(self):
        self.cwd = os.path.dirname(__file__)
        self.client = Client(self.app, Response)

