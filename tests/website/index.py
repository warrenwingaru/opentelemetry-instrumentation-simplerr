import simplerr
from simplerr import GET


def hello_endpoint(request, helloid):
    if helloid == 500:
        raise ValueError(":-(")
    return f"Hello: {helloid}"


@simplerr.web("/hello/<int:helloid>", GET)
def hello_id(request, helloid):
    return hello_endpoint(request, helloid)


@simplerr.web("/excluded/<int:helloid>")
def excluded_helloid(request, helloid):
    return hello_endpoint(request, helloid)


@simplerr.web("/excluded")
def excluded_endpoint():
    return 'excluded'


@simplerr.web("/excluded2")
def excluded2_endpoint():
    return "excluded2"

# @simplerr.web('/assert_environ')
# def assert_environ(request):
#
#
