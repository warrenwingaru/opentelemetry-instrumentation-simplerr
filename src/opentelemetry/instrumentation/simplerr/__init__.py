"""
This library builds on the OpenTelemetry WSGI middleware to track web requests
in Simplerr applications.
"""
import json
from logging import getLogger
from timeit import default_timer
from typing import Collection

import opentelemetry.instrumentation.wsgi as otel_wsgi
import simplerr
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.propagators import (
    get_global_response_propagator,
)
from opentelemetry.instrumentation.utils import _start_internal_or_server_span
from opentelemetry.metrics import get_meter
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.util._time import _time_ns
from opentelemetry.util.http import parse_excluded_urls, get_excluded_urls
from simplerr import dispatcher, script
from werkzeug.exceptions import NotFound, HTTPException
from werkzeug.http import HTTP_STATUS_CODES

from opentelemetry import context, trace
from opentelemetry.instrumentation.simplerr.package import _instruments
from opentelemetry.instrumentation.simplerr.version import __version__

_logger = getLogger(__name__)

_ENVIRON_STARTTIME_KEY = "opentelemetry-simplerr.starttime_key"
_ENVIRON_SPAN_KEY = "opentelemetry-simplerr.span_key"
_ENVIRON_ACTIVATION_KEY = "opentelemetry-simplerr.activation_key"
_ENVIRON_TOKEN = "opentelemetry-simplerr.token"

_excluded_urls_from_env = get_excluded_urls("SIMPLERR")


def get_default_span_name(environ):
    method = environ.get("REQUEST_METHOD", "")
    if method == "_OTHER":
        method = "HTTP"
    try:
        span_name = f"{method} {environ['PATH_INFO']}"
    except AttributeError:
        span_name = otel_wsgi.get_default_span_name(environ)
    return span_name


def _rewrapped_app(
        wsgi_app,
        active_request_counter,
        duration_histogram,
        excluded_urls=None,
):
    def _wrapped_app(wrapped_app_environ, start_response):
        wrapped_app_environ[_ENVIRON_STARTTIME_KEY] = _time_ns()

        start = default_timer()
        attributes = otel_wsgi.collect_request_attributes(wrapped_app_environ)
        active_requests_count_attrs = (
            otel_wsgi._parse_active_request_count_attrs(attributes)
        )
        duration_attrs = otel_wsgi._parse_duration_attrs(attributes)
        active_request_counter.add(1, active_requests_count_attrs)

        request_route = wrapped_app_environ.get("PATH_INFO", None)

        def _start_response(status, response_headers, *args, **kwargs):
            if excluded_urls is None or not excluded_urls.url_disabled(request_route):
                span = wrapped_app_environ.get(_ENVIRON_SPAN_KEY)

                propagator = get_global_response_propagator()
                if propagator:
                    propagator.inject(
                        response_headers,
                        setter=otel_wsgi.default_response_propagation_setter
                    )
                headers_dict = dict(response_headers)
                _status = headers_dict.get('x-json-status', status)
                if span:
                    otel_wsgi.add_response_attributes(
                        span,
                        _status,
                        response_headers,
                    )
                    status_code = otel_wsgi._parse_status_code(_status)
                    if status_code is not None:
                        duration_attrs[SpanAttributes.HTTP_STATUS_CODE] = status_code
                    if (
                            span.is_recording()
                            and span.kind == trace.SpanKind.SERVER
                    ):
                        custom_attributes = otel_wsgi.collect_custom_response_headers_attributes(response_headers)
                        if len(custom_attributes) > 0:
                            span.set_attributes(custom_attributes)
                else:
                    _logger.warning(
                        "Simplerr environ's OpenTelemetry span ",
                        "missing at _start_response(%s)",
                        _status,
                    )

            return start_response(
                status,
                response_headers,
                *args,
                **kwargs)

        result = wsgi_app(wrapped_app_environ, _start_response)
        duration_s = default_timer() - start

        if request_route:
            duration_attrs[SpanAttributes.HTTP_ROUTE] = str(request_route)

        duration_histogram.record(max(round(duration_s * 1000 ), 0), duration_attrs)
        active_request_counter.add(-1, active_requests_count_attrs)
        return result

    return _wrapped_app


class _PatchedWebEvents(simplerr.dispatcher.WebEvents):
    def fire_post_response(self, request, response, exc=None):
        for fn in self.post_request:
            fn(request, response, exc)


# Todo add to the main code someday, keeping it clean with otel instrumentation
class _PatchedDispatcher(dispatcher.dispatcher):
    # Recreating the dispatch request
    def __call__(self, environ, start_response):
        request = simplerr.dispatcher.WebRequest(environ)

        self.global_events.fire_pre_response(request)
        request.view_events.fire_pre_response(request)
        exc = None

        try:
            simplerr.web.restore_presets()

            sc = script.script(self.cwd, request.path, extension=self.extension)
            sc.get_module()

            response = simplerr.web.process(request, environ, self.cwd)
        except (NotFound, OSError) as e:
            exc = e
            response = NotFound().get_response(environ)
        except HTTPException as e:
            exc = e
            response = e.get_response(environ)

        if 'application/json' in response.mimetype and response.method not in 'HEAD OPTIONS':
            actual_status_code = getattr(response, "status_code", None)
            body = json.loads(response.get_data())
            json_status_code = body.get('status', None)
            try:
                if json_status_code:
                    if int(actual_status_code) != int(json_status_code):
                        _logger.warning(f'Response has HTTP {actual_status_code} but JSON has {json_status_code}')
                    response.headers.add("x-json-status",
                                         f'{json_status_code} {HTTP_STATUS_CODES.get(json_status_code, "Unknown")}')
            except ValueError:
                _logger.warning(
                    f'Failed to parse HTTP status code {actual_status_code} or JSON status code {json_status_code} as integers')

        result = response(environ, start_response)

        request.view_events.fire_post_response(request, response)
        self.global_events.fire_post_response(request, response, exc)
        return result


class _InstrumentedWsgi(dispatcher.wsgi):
    _excluded_urls = None
    _enable_commenter = True
    _commenter_options = None
    _meter_provider = None
    _trace_provider = None

    def make_app(self):
        self.app = super().make_app()
        self.app = _rewrapped_app(self.app, self.active_request_counter,
                                  duration_histogram=self.duration_histogram,
                                  excluded_urls=_InstrumentedWsgi._excluded_urls)
        return self.app

    def __init__(self, *args, **kwargs):
        super(_InstrumentedWsgi, self).__init__(*args, **kwargs)

        self._original_app = self.app
        self._is_instrumented_by_opentelemetry = True

        meter = get_meter(
            __name__,
            __version__,
            _InstrumentedWsgi._meter_provider,
        )
        self.duration_histogram = meter.create_histogram(
            name="http.server.duration",
            unit="ms",
            description="measures the duration of the inbound HTTP request",
        )
        self.active_request_counter = meter.create_up_down_counter(
            name="http.server.active_requests",
            unit="requests",
            description="measures the number of concurrent HTTP requests that are currently in-flight"
        )
        tracer = trace.get_tracer(
            __name__,
            __version__,
            _InstrumentedWsgi._trace_provider,
        )

        def pre_response(request):
            excluded_urls = _InstrumentedWsgi._excluded_urls
            simplerr_request_environ = request.environ
            request_route = simplerr_request_environ.get("PATH_INFO", None)

            if excluded_urls and excluded_urls.url_disabled(request_route):
                return

            span_name = get_default_span_name(simplerr_request_environ)
            attributes = otel_wsgi.collect_request_attributes(simplerr_request_environ)
            if request_route:
                attributes[SpanAttributes.HTTP_ROUTE] = str(request_route)

            span, token = _start_internal_or_server_span(
                tracer=tracer,
                span_name=span_name,
                start_time=simplerr_request_environ.get(_ENVIRON_STARTTIME_KEY),
                context_carrier=simplerr_request_environ,
                context_getter=otel_wsgi.wsgi_getter,
                attributes=attributes
            )
            if span.is_recording():
                for key, value in attributes.items():
                    span.set_attribute(key, value)
                if span.is_recording() and span.kind == trace.SpanKind.SERVER:
                    custom_attributes = otel_wsgi.collect_custom_request_headers_attributes(
                        simplerr_request_environ,
                    )

                    if len(custom_attributes) > 0:
                        span.set_attributes(custom_attributes)
            activation = trace.use_span(span, end_on_exit=True)
            activation.__enter__()
            simplerr_request_environ[_ENVIRON_ACTIVATION_KEY] = activation
            simplerr_request_environ[_ENVIRON_SPAN_KEY] = span
            simplerr_request_environ[_ENVIRON_TOKEN] = token

        def post_response(request, response, exc):
            excluded_urls = _InstrumentedWsgi._excluded_urls
            simplerr_request_environ = request.environ
            request_route = simplerr_request_environ.get("PATH_INFO", None)

            if excluded_urls and excluded_urls.url_disabled(request_route):
                return

            activation = request.environ.get(_ENVIRON_ACTIVATION_KEY)
            if not activation:
                return
            if exc is None:
                activation.__exit__(None, None, None)
            else:
                activation.__exit__(type(exc), exc, getattr(exc, "__traceback__", None))

            if request.environ.get(_ENVIRON_TOKEN, None):
                context.detach(request.environ[_ENVIRON_TOKEN])

        self.global_events = _PatchedWebEvents()
        self.global_events.on_pre_response(pre_response)
        self.global_events.on_post_response(post_response)


class SimplerrInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        self._original_wsgi = dispatcher.wsgi

        tracer_provider = kwargs.get('tracer_provider')
        _InstrumentedWsgi._tracer_provider = tracer_provider
        excluded_urls = kwargs.get('excluded_urls')
        _InstrumentedWsgi._excluded_urls = (
            _excluded_urls_from_env
            if excluded_urls is None
            else parse_excluded_urls(excluded_urls)
        )

        enable_commenter = kwargs.get('enable_commenter', True)
        _InstrumentedWsgi._enable_commenter = enable_commenter
        commenter_options = kwargs.get('commenter_options', {})
        _InstrumentedWsgi._commenter_options = commenter_options
        meter_provider = kwargs.get('meter_provider')
        _InstrumentedWsgi._meter_provider = meter_provider

        simplerr.dispatcher.dispatcher = _PatchedDispatcher
        simplerr.dispatcher.wsgi = _InstrumentedWsgi

    def _uninstrument(self, **kwargs):
        simplerr.dispatcher.wsgi = self._original_wsgi
