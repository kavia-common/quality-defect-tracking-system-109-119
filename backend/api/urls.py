from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CorrectiveActionViewSet,
    DefectViewSet,
    RootCauseViewSet,
    export_csv_raw,
    health,
    resolve_user,
)

router = DefaultRouter()
router.register(r"defects", DefectViewSet, basename="defect")
router.register(r"root-causes", RootCauseViewSet, basename="root-cause")
router.register(r"corrective-actions", CorrectiveActionViewSet, basename="corrective-action")

urlpatterns = [
    path("health/", health, name="Health"),
    path("users/resolve/", resolve_user, name="ResolveUser"),
    path("export-csv", export_csv_raw, name="ExportCsvRaw"),
    path("", include(router.urls)),
]
