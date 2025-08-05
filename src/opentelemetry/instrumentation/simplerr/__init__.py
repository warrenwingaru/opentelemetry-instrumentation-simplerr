"""
This library builds on the OpenTelemetry WSGI middleware to track web requests
in Simplerr applications.
"""
from logging import getLogger
from time import time_ns
from timeit import default_timer
from typing import Collection

import simplerr
import simplerr.dispatcher

import opentelemetry.instrumentation.wsgi as otel_wsgi
from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability,
    _OpenTelemetryStabilitySignalType,
    _get_schema_url,
    _report_old,
    _report_new,
    _StabilityMode, HTTP_DURATION_HISTOGRAM_BUCKETS_NEW
)
from opentelemetry import context, trace
from opentelemetry.instrumentation.simplerr.package import _instruments
from opentelemetry.instrumentation.simplerr.version import __version__
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.propagators import (
    get_global_response_propagator,
)
from opentelemetry.instrumentation.utils import _start_internal_or_server_span
from opentelemetry.metrics import get_meter
from opentelemetry.semconv.attributes.http_attributes import HTTP_ROUTE
from opentelemetry.semconv.metrics import MetricInstruments
from opentelemetry.semconv.metrics.http_metrics import (
    HTTP_SERVER_REQUEST_DURATION
)
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.util._importlib_metadata import version
from opentelemetry.util.http import (
    parse_excluded_urls,
    get_excluded_urls,
    sanitize_method
)

_logger = getLogger(__name__)

_ENVIRON_STARTTIME_KEY = "opentelemetry-simplerr.starttime_key"
_ENVIRON_SPAN_KEY = "opentelemetry-simplerr.span_key"
_ENVIRON_ACTIVATION_KEY = "opentelemetry-simplerr.activation_key"
_ENVIRON_TOKEN = "opentelemetry-simplerr.token"

_excluded_urls_from_env = get_excluded_urls("SIMPLERR")

simplerr_version = version("simplerr")


def get_default_span_name(request):
    method = sanitize_method(
        request.environ.get("REQUEST_METHOD", "").strip()
    )
    if method == "_OTHER":
        method = "HTTP"
    try:
        span_name = f"{method} {request.url_rule.rule}"
    except AttributeError:
        span_name = otel_wsgi.get_default_span_name(request.environ)
    return span_name


def _rewrapped_app(
        wsgi_app,
        active_request_counter,
        duration_histogram_old=None,
        duration_histogram_new=None,
        excluded_urls=None,
        sem_conv_opt_in_mode=None,
):
    def _wrapped_app(wrapped_app_environ, start_response):
        wrapped_app_environ[_ENVIRON_STARTTIME_KEY] = time_ns()

        start = default_timer()
        attributes = otel_wsgi.collect_request_attributes(
            wrapped_app_environ, sem_conv_opt_in_mode
        )
        active_requests_count_attrs = (
            otel_wsgi._parse_active_request_count_attrs(
                attributes,
                sem_conv_opt_in_mode
            )
        )
        active_request_counter.add(1, active_requests_count_attrs)

        request_route = None

        def _start_response(status, response_headers, *args, **kwargs):
            url_rule = wrapped_app_environ.get("simplerr.url_rule", None)
            if (
                    excluded_urls is None
                    or not excluded_urls.url_disabled(wrapped_app_environ.get('PATH_INFO', None))
            ):
                nonlocal request_route
                if url_rule:
                    request_route = url_rule.rule

                span = wrapped_app_environ.get(_ENVIRON_SPAN_KEY)

                propagator = get_global_response_propagator()
                if propagator:
                    propagator.inject(
                        response_headers,
                        setter=otel_wsgi.default_response_propagation_setter
                    )
                if span:
                    otel_wsgi.add_response_attributes(
                        span,
                        status,
                        response_headers,
                        attributes,
                        sem_conv_opt_in_mode
                    )
                    if (
                            span.is_recording()
                            and span.kind == trace.SpanKind.SERVER
                    ):
                        custom_attributes = otel_wsgi.collect_custom_response_headers_attributes(response_headers)
                        if len(custom_attributes) > 0:
                            span.set_attributes(custom_attributes)
                else:
                    _logger.warning(
                        "Simplerr environ's OpenTelemetry span"
                        " missing at _start_response(%s)",
                        status,
                    )

            return start_response(
                status,
                response_headers,
                *args,
                **kwargs)

        result = wsgi_app(wrapped_app_environ, _start_response)
        duration_s = default_timer() - start
        if duration_histogram_old:
            duration_attrs_old = otel_wsgi._parse_duration_attrs(
                attributes, _StabilityMode.DEFAULT
            )

            if request_route:
                duration_attrs_old[SpanAttributes.HTTP_TARGET] = str(request_route)

            duration_histogram_old.record(
                max(round(duration_s * 1000), 0), duration_attrs_old
            )

        if duration_histogram_new:
            duration_attrs_new = otel_wsgi._parse_duration_attrs(
                attributes, _StabilityMode.HTTP
            )

            if request_route:
                duration_attrs_new[HTTP_ROUTE] = str(request_route)

            duration_histogram_new.record(
                max(duration_s, 0), duration_attrs_new
            )

        active_request_counter.add(-1, active_requests_count_attrs)
        return result

    return _wrapped_app


class _InstrumentedWsgi(simplerr.dispatcher.wsgi):
    _excluded_urls = None
    _enable_commenter = True
    _commenter_options = None
    _meter_provider = None
    _tracer_provider = None
    _sem_conv_opt_in_mode = None

    def __init__(self, *args, **kwargs):
        super(_InstrumentedWsgi, self).__init__(*args, **kwargs)

        self._original_wsgi_app = self.wsgi_app
        self._is_instrumented_by_opentelemetry = True

        meter = get_meter(
            __name__,
            __version__,
            _InstrumentedWsgi._meter_provider,
            schema_url=_get_schema_url(_InstrumentedWsgi._sem_conv_opt_in_mode),
        )
        duration_histogram_old = None
        if _report_old(_InstrumentedWsgi._sem_conv_opt_in_mode):
            duration_histogram_old = meter.create_histogram(
                name=MetricInstruments.HTTP_SERVER_DURATION,
                unit="ms",
                description="measures the duration of the inbound HTTP request",
            )

        duration_histogram_new = None
        if _report_new(_InstrumentedWsgi._sem_conv_opt_in_mode):
            duration_histogram_new = meter.create_histogram(
                name=HTTP_SERVER_REQUEST_DURATION,
                unit="s",
                description="Duration of HTTP server requests.",
                explicit_bucket_boundaries_advisory=HTTP_DURATION_HISTOGRAM_BUCKETS_NEW
            )

        active_request_counter = meter.create_up_down_counter(
            name=MetricInstruments.HTTP_SERVER_ACTIVE_REQUESTS,
            unit="requests",
            description="measures the number of concurrent HTTP requests that are currently in-flight"
        )
        self.wsgi_app = _rewrapped_app(self.wsgi_app, active_request_counter,
                                       duration_histogram_old=duration_histogram_old,
                                       duration_histogram_new=duration_histogram_new,
                                       sem_conv_opt_in_mode=_InstrumentedWsgi._sem_conv_opt_in_mode,
                                       excluded_urls=_InstrumentedWsgi._excluded_urls)

        tracer = trace.get_tracer(
            __name__,
            __version__,
            _InstrumentedWsgi._tracer_provider,
            schema_url=_get_schema_url(_InstrumentedWsgi._sem_conv_opt_in_mode),
        )

        self._post_response = _wrapped_post_response(excluded_urls=_InstrumentedWsgi._excluded_urls, )
        self._pre_response = _wrapped_pre_response(tracer=tracer, excluded_urls=_InstrumentedWsgi._excluded_urls,
                                                   enable_commenter=_InstrumentedWsgi._enable_commenter,
                                                   commenter_options=_InstrumentedWsgi._commenter_options,
                                                   sem_conv_opt_in_mode=_InstrumentedWsgi._sem_conv_opt_in_mode)

        self.global_events.on_pre_response(self._pre_response)
        self.global_events.on_teardown_request(self._post_response)


class SimplerrInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    def _instrument(self, **kwargs):
        self._original_wsgi = simplerr.dispatcher.wsgi

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

        sem_conv_opt_in_mode = _OpenTelemetrySemanticConventionStability._get_opentelemetry_stability_opt_in_mode(
            _OpenTelemetryStabilitySignalType.HTTP,
        )

        _InstrumentedWsgi._sem_conv_opt_in_mode = sem_conv_opt_in_mode

        simplerr.dispatcher.wsgi = _InstrumentedWsgi

    def _uninstrument(self, **kwargs):
        simplerr.dispatcher.wsgi = self._original_wsgi

    @staticmethod
    def instrument_app(app, tracer_provider=None, excluded_urls=None, meter_provider=None, enable_commenter=True,
                       commenter_options=None):
        if not hasattr(app, '_is_instrumented_by_opentelemetry'):
            app._is_instrumented_by_opentelemetry = False

        if not app._is_instrumented_by_opentelemetry:
            # initialize semantic conventions opt-in if needed
            _OpenTelemetrySemanticConventionStability._initialize()
            sem_conv_opt_in_mode = _OpenTelemetrySemanticConventionStability._get_opentelemetry_stability_opt_in_mode(
                _OpenTelemetryStabilitySignalType.HTTP
            )
            excluded_urls = (
                parse_excluded_urls(excluded_urls)
                if excluded_urls is not None
                else _excluded_urls_from_env
            )
            meter = get_meter(
                __name__,
                __version__,
                meter_provider,
                schema_url=_get_schema_url(sem_conv_opt_in_mode)
            )
            duration_histogram_old = None
            if _report_old(sem_conv_opt_in_mode):
                duration_histogram_old = meter.create_histogram(
                    name=MetricInstruments.HTTP_SERVER_DURATION,
                    unit="ms",
                    description="measures the duration of the inbound HTTP request",
                )

            duration_histogram_new = None
            if _report_new(sem_conv_opt_in_mode):
                duration_histogram_new = meter.create_histogram(
                    name=HTTP_SERVER_REQUEST_DURATION,
                    unit="s",
                    description="Duration of HTTP server requests.",
                    explicit_bucket_boundaries_advisory=HTTP_DURATION_HISTOGRAM_BUCKETS_NEW
                )

            active_request_counter = meter.create_up_down_counter(
                name=MetricInstruments.HTTP_SERVER_ACTIVE_REQUESTS,
                unit="requests",
                description="measures the number of concurrent HTTP requests that are currently in-flight"
            )
            app._original_wsgi_app = app.wsgi_app

            app.wsgi_app = _rewrapped_app(
                app.wsgi_app,
                active_request_counter,
                duration_histogram_old=duration_histogram_old,
                excluded_urls=excluded_urls,
                sem_conv_opt_in_mode=sem_conv_opt_in_mode,
                duration_histogram_new=duration_histogram_new
            )

            tracer = trace.get_tracer(
                __name__,
                __version__,
                tracer_provider,
                schema_url=_get_schema_url(sem_conv_opt_in_mode)
            )

            _post_response = _wrapped_post_response(excluded_urls=excluded_urls)
            _pre_response = _wrapped_pre_response(
                tracer=tracer,
                excluded_urls=excluded_urls,
                enable_commenter=enable_commenter,
                commenter_options=(
                    commenter_options if commenter_options else {}
                ),
                sem_conv_opt_in_mode=sem_conv_opt_in_mode
            )

            app._post_response = _post_response
            app._pre_response = _pre_response

            app.global_events.on_pre_response(_pre_response)
            app.global_events.on_teardown_request(_post_response)
            app._is_instrumented_by_opentelemetry = True
        else:
            _logger.warning("Attempting to instrument Simplerr app while already instrumented")

    @staticmethod
    def uninstrument_app(wsgi):
        if hasattr(wsgi, '_original_wsgi_app'):
            wsgi.wsgi_app = wsgi._original_wsgi_app

            wsgi.global_events.off_pre_response(wsgi._pre_response)
            wsgi.global_events.off_teardown_request(wsgi._post_response)
            del wsgi._original_wsgi_app
            wsgi._is_instrumented_by_opentelemetry = False
        else:
            _logger.warning("Attempting to uninstrument Simplerr "
                            "app while already uninstrumented")


def _wrapped_pre_response(
        tracer=None,
        excluded_urls=None,
        enable_commenter=True,
        commenter_options=None,
        sem_conv_opt_in_mode=_StabilityMode.DEFAULT,
):
    def _pre_response(request):
        if excluded_urls and excluded_urls.url_disabled(request.url):
            return

        simplerr_request_environ = request.environ
        span_name = get_default_span_name(request)

        attributes = otel_wsgi.collect_request_attributes(
            simplerr_request_environ,
            sem_conv_opt_in_mode
        )

        if request.url_rule:
            # For 404 that result from no route found, etc, we
            # don't have a url_rule
            attributes[SpanAttributes.HTTP_ROUTE] = str(request.url_rule.rule)

        span, token = _start_internal_or_server_span(
            tracer=tracer,
            span_name=span_name,
            start_time=simplerr_request_environ.get(_ENVIRON_STARTTIME_KEY),
            context_carrier=simplerr_request_environ,
            context_getter=otel_wsgi.wsgi_getter,
            attributes=attributes,
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

        if enable_commenter:
            current_context = context.get_current()
            simplerr_info = {}

            if commenter_options.get("framework", True):
                simplerr_info["framework"] = f"simplerr:{simplerr_version}"
            if (
                    commenter_options.get('controller', True)
                    and request.url_rule
                    and request.url_rule.endpoint
            ):
                simplerr_info["controller"] = request.url_rule.endpoint
            if (
                    commenter_options.get('route', True)
                    and request.url_rule
                    and request.url_rule.rule
            ):
                simplerr_info["route"] = request.url_rule.rule

            sqlcommenter_context = context.set_value(
                "SQLCOMMENTER_ORM_TAGS_AND_VALUES", simplerr_info, current_context
            )
            context.attach(sqlcommenter_context)

    return _pre_response


def _wrapped_post_response(
        excluded_urls=None,
):
    def _post_response(request, exc):
        if excluded_urls and excluded_urls.url_disabled(request.url):
            return
        simplerr_request_environ = request.environ

        activation = simplerr_request_environ.get(_ENVIRON_ACTIVATION_KEY)
        if not activation:
            return
        if exc is None:
            activation.__exit__(None, None, None)
        else:
            activation.__exit__(type(exc), exc, getattr(exc, "__traceback__", None))

        if simplerr_request_environ.get(_ENVIRON_TOKEN, None):
            context.detach(simplerr_request_environ[_ENVIRON_TOKEN])

    return _post_response
