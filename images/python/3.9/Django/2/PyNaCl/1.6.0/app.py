import sys
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="dev-secret-key-not-for-production",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=[],
    )
    django.setup()

from django.http import JsonResponse
try:
    from django.urls import path as _route
    _urlpatterns = lambda h, v: [_route("", h), _route("version", v)]
except ImportError:
    from django.conf.urls import url as _route
    _urlpatterns = lambda h, v: [_route(r"^$", h), _route(r"^version$", v)]
import nacl


def hello(request):
    return JsonResponse({"message": "Hello World"})


def version_view(request):
    lib_version = nacl.__version__
    return JsonResponse({
        "language": {"name": "Python", "version": sys.version.split()[0]},
        "framework": {"name": "Django", "version": django.__version__},
        "library": {"name": "PyNaCl", "version": str(lib_version)},
    })


urlpatterns = _urlpatterns(hello, version_view)

if __name__ == "__main__":
    from django.core.management import execute_from_command_line
    execute_from_command_line(["manage.py", "runserver", "--noreload", "0.0.0.0:8000"])
