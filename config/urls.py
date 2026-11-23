from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def api_root(request):
    """The UI is served by the Vite frontend (http://localhost:5173); this is only the API."""
    return JsonResponse(
        {
            "service": "Installment Ledger API",
            "status": "ok",
            "frontend": "http://localhost:5173",
            "endpoints": ["/api/auth/", "/api/plans/", "/api/summary/", "/admin/"],
        }
    )


urlpatterns = [
    path("", api_root),
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("ledger.urls")),
]
