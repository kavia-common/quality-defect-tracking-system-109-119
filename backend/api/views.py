from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta

from django.db.models import Count, QuerySet
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.renderers import BaseRenderer
from django.contrib.auth import get_user_model
from rest_framework.request import Request
from rest_framework.response import Response

from .models import CorrectiveAction, Defect, DefectStatusHistory, RootCause
from .serializers import CorrectiveActionSerializer, DefectSerializer, RootCauseSerializer


@api_view(["GET"])
def health(request: Request) -> Response:
    """Health check endpoint used by deployment and tests."""
    return Response({"message": "Server is up!"})


@api_view(["POST"])
def resolve_user(request: Request) -> Response:
    """Resolve (or create) a user by username.

    This supports the lightweight demo UI which captures corrective-action owner as
    free-text. Backend corrective actions require `owner` to be a valid user id,
    so the frontend can call this endpoint first to obtain an id.

    Body:
      { "username": "<string>" }

    Response:
      { "id": <int>, "username": "<string>" }
    """
    User = get_user_model()
    username = (request.data.get("username") or "").strip()
    if not username:
        return Response({"username": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Keep it simple: create user if missing; no password required for this demo.
    user, _created = User.objects.get_or_create(username=username, defaults={"email": ""})
    # Return 201 to match the documented OpenAPI contract for this endpoint.
    return Response({"id": user.id, "username": user.username}, status=status.HTTP_201_CREATED)


def _dt_floor_utc(d: date) -> datetime:
    """Return start-of-day datetime for a date in UTC."""
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _dt_ceil_utc(d: date) -> datetime:
    """Return end-of-day datetime for a date in UTC."""
    # Inclusive end-of-day: 23:59:59.999999
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


class CSVRenderer(BaseRenderer):
    """
    Minimal DRF renderer for CSV responses.

    DRF viewsets/actions still perform content negotiation. When an action returns
    a Django HttpResponse (text/csv) but the request 'Accept' header prefers JSON,
    DRF can raise 406: "Could not satisfy the request Accept header."

    By declaring a renderer for 'text/csv' on the action, we make negotiation
    deterministic and avoid 406 while still returning a streamed CSV file.
    """

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # We return an HttpResponse directly in the view, so this should not be used,
        # but DRF requires a renderer to satisfy negotiation. Keep it safe anyway.
        if data is None:
            return b""
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return str(data).encode(self.charset)


class DefectViewSet(viewsets.ModelViewSet):
    """
    Defect CRUD + filtering + reporting/export endpoints.

    Filtering query params:
    - status: Defect status (OPEN, INVESTIGATING, ...)
    - severity: LOW|MEDIUM|HIGH|CRITICAL
    - assignee: user id (integer)
    - reported_by: user id (integer)
    - created_from / created_to: ISO datetime or YYYY-MM-DD
    - occurred_from / occurred_to: ISO datetime or YYYY-MM-DD

    Reporting endpoints:
    - GET /api/defects/report/metrics/
    - GET /api/defects/report/pareto/
    - GET /api/defects/report/trend/
    - GET /api/defects/{id}/audit-export/ (CSV)
    """

    serializer_class = DefectSerializer
    queryset = Defect.objects.all().select_related("assignee", "reported_by").select_related("root_cause")

    def _parse_dt_or_date(self, value: str):
        dt = parse_datetime(value)
        if dt:
            # If client sends naive datetime, treat it as UTC.
            if timezone.is_naive(dt):
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        d = parse_date(value)
        if d:
            return d
        return None

    def _apply_datetime_range(self, qs: QuerySet[Defect], *, field: str, start: str | None, end: str | None):
        if start:
            parsed = self._parse_dt_or_date(start)
            if isinstance(parsed, datetime):
                qs = qs.filter(**{f"{field}__gte": parsed})
            elif isinstance(parsed, date):
                qs = qs.filter(**{f"{field}__gte": _dt_floor_utc(parsed)})
        if end:
            parsed = self._parse_dt_or_date(end)
            if isinstance(parsed, datetime):
                qs = qs.filter(**{f"{field}__lte": parsed})
            elif isinstance(parsed, date):
                qs = qs.filter(**{f"{field}__lte": _dt_ceil_utc(parsed)})
        return qs

    def get_queryset(self) -> QuerySet[Defect]:
        qs = super().get_queryset().order_by("-created_at")

        params = self.request.query_params
        status_val = params.get("status")
        severity_val = params.get("severity")
        assignee_val = params.get("assignee")
        reported_by_val = params.get("reported_by")

        if status_val:
            qs = qs.filter(status=status_val)
        if severity_val:
            qs = qs.filter(severity=severity_val)
        if assignee_val:
            qs = qs.filter(assignee_id=assignee_val)
        if reported_by_val:
            qs = qs.filter(reported_by_id=reported_by_val)

        qs = self._apply_datetime_range(
            qs,
            field="created_at",
            start=params.get("created_from"),
            end=params.get("created_to"),
        )
        qs = self._apply_datetime_range(
            qs,
            field="occurred_at",
            start=params.get("occurred_from"),
            end=params.get("occurred_to"),
        )
        return qs

    def perform_create(self, serializer: DefectSerializer) -> None:
        defect = serializer.save()
        # Ensure an initial history entry exists for new defects.
        DefectStatusHistory.objects.create(
            defect=defect,
            from_status="",
            to_status=defect.status,
            changed_by=getattr(self.request, "user", None) if getattr(self.request, "user", None) and self.request.user.is_authenticated else None,
            note="Created defect.",
        )
        # Ensure root cause row exists to support RCA UI. (Backfill migration does it for existing rows.)
        RootCause.objects.get_or_create(defect=defect)

    def perform_update(self, serializer: DefectSerializer) -> None:
        defect: Defect = self.get_object()
        old_status = defect.status
        updated: Defect = serializer.save()

        if "status" in serializer.validated_data and updated.status != old_status:
            DefectStatusHistory.objects.create(
                defect=updated,
                from_status=old_status,
                to_status=updated.status,
                changed_by=getattr(self.request, "user", None) if getattr(self.request, "user", None) and self.request.user.is_authenticated else None,
                note="Status changed via update.",
            )

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request: Request, pk: str | None = None) -> Response:
        """Transition a defect status.

        Body:
        {
          "status": "<new_status>"
        }
        """
        defect = self.get_object()
        new_status = request.data.get("status")
        if not new_status:
            return Response({"status": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(
            defect, data={"status": new_status}, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # perform_update already logged history when `status` changed, but transition uses serializer.save()
        # which calls perform_update; we keep this endpoint compatible with that behavior.
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="report/metrics")
    def report_metrics(self, request: Request) -> Response:
        """Return dashboard metric counts.

        Query params (optional):
        - status, severity: to scope metrics to a subset
        """
        qs = self.get_queryset()

        total_defects = qs.count()
        open_defects = qs.filter(status__in=[Defect.Status.OPEN, Defect.Status.INVESTIGATING, Defect.Status.ACTIONS_IN_PROGRESS]).count()
        closed_defects = qs.filter(status=Defect.Status.CLOSED).count()

        today = timezone.now().date()
        overdue_actions = CorrectiveAction.objects.filter(
            defect__in=qs.values_list("id", flat=True),
            due_date__lt=today,
        ).exclude(
            status__in=[CorrectiveAction.Status.DONE, CorrectiveAction.Status.VERIFIED, CorrectiveAction.Status.CANCELED]
        ).exclude(completed_at__isnull=False)

        return Response(
            {
                "total_defects": total_defects,
                "open_defects": open_defects,
                "closed_defects": closed_defects,
                "overdue_actions_count": overdue_actions.count(),
            }
        )

    @action(detail=False, methods=["get"], url_path="report/pareto")
    def report_pareto(self, request: Request) -> Response:
        """Return a simple Pareto-style ranking of defects.

        We interpret "defect types" as defect titles for this lightweight demo.
        Response:
          [{ "label": "<title>", "count": <n> }, ...]
        """
        qs = self.get_queryset()
        top_n = int(request.query_params.get("top", "10") or "10")
        rows = (
            qs.values("title")
            .annotate(count=Count("id"))
            .order_by("-count", "title")[: max(1, min(top_n, 50))]
        )
        return Response([{"label": r["title"], "count": r["count"]} for r in rows])

    @action(detail=False, methods=["get"], url_path="report/trend")
    def report_trend(self, request: Request) -> Response:
        """Return defect creation trend counts.

        Query params:
        - window_days (default 30)
        Response:
          [{ "date": "YYYY-MM-DD", "count": <n> }, ...]
        """
        window_days = int(request.query_params.get("window_days", "30") or "30")
        window_days = max(1, min(window_days, 365))

        end = timezone.now().date()
        start = end - timedelta(days=window_days - 1)

        qs = self.get_queryset().filter(created_at__gte=_dt_floor_utc(start), created_at__lte=_dt_ceil_utc(end))

        # SQLite-friendly grouping: we build buckets in Python.
        # For the expected dataset size in this demo, this is fine.
        buckets = {start + timedelta(days=i): 0 for i in range(window_days)}
        for created in qs.values_list("created_at", flat=True):
            d = created.date()
            if start <= d <= end:
                buckets[d] += 1

        return Response(
            [{"date": d.isoformat(), "count": buckets[d]} for d in sorted(buckets.keys())]
        )

    @action(detail=True, methods=["get"], url_path="audit-export", renderer_classes=[CSVRenderer])
    def audit_export(self, request: Request, pk: str | None = None) -> HttpResponse:
        """Export a defect audit report as CSV.

        Includes:
        - defect details
        - root cause (including 5-Why)
        - corrective actions
        - status history
        """
        defect = self.get_object()
        root_cause = getattr(defect, "root_cause", None)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="defect_{defect.id}_audit.csv"'

        writer = csv.writer(response)

        writer.writerow(["SECTION", "FIELD", "VALUE"])
        writer.writerow(["defect", "id", defect.id])
        writer.writerow(["defect", "title", defect.title])
        writer.writerow(["defect", "description", defect.description])
        writer.writerow(["defect", "severity", defect.severity])
        writer.writerow(["defect", "status", defect.status])
        writer.writerow(["defect", "priority", defect.priority])
        writer.writerow(["defect", "area", defect.area])
        writer.writerow(["defect", "tags", ",".join(defect.tags or [])])
        writer.writerow(["defect", "reporter_name", defect.reporter_name])
        writer.writerow(["defect", "assigned_to_name", defect.assigned_to_name])
        writer.writerow(["defect", "occurred_at", defect.occurred_at.isoformat() if defect.occurred_at else ""])
        writer.writerow(["defect", "due_date", defect.due_date.isoformat() if defect.due_date else ""])
        writer.writerow(["defect", "created_at", defect.created_at.isoformat() if defect.created_at else ""])
        writer.writerow(["defect", "updated_at", defect.updated_at.isoformat() if defect.updated_at else ""])

        # Root cause section
        if root_cause:
            writer.writerow(["root_cause", "status", root_cause.status])
            writer.writerow(["root_cause", "summary", root_cause.summary])
            writer.writerow(["root_cause", "analysis", root_cause.analysis])
            writer.writerow(["root_cause", "why_1", root_cause.why_1])
            writer.writerow(["root_cause", "why_2", root_cause.why_2])
            writer.writerow(["root_cause", "why_3", root_cause.why_3])
            writer.writerow(["root_cause", "why_4", root_cause.why_4])
            writer.writerow(["root_cause", "why_5", root_cause.why_5])
            writer.writerow(
                [
                    "root_cause",
                    "identified_by",
                    root_cause.identified_by.username if root_cause.identified_by else "",
                ]
            )
            writer.writerow(
                ["root_cause", "identified_at", root_cause.identified_at.isoformat() if root_cause.identified_at else ""]
            )

        # Corrective actions
        writer.writerow(["corrective_actions", "count", defect.corrective_actions.count()])
        for action_obj in defect.corrective_actions.all().order_by("id"):
            writer.writerow(["corrective_action", "id", action_obj.id])
            writer.writerow(["corrective_action", "title", action_obj.title])
            writer.writerow(["corrective_action", "description", action_obj.description])
            writer.writerow(["corrective_action", "status", action_obj.status])
            writer.writerow(["corrective_action", "owner", action_obj.owner.username if action_obj.owner else ""])
            writer.writerow(["corrective_action", "due_date", action_obj.due_date.isoformat() if action_obj.due_date else ""])
            writer.writerow(
                ["corrective_action", "completed_at", action_obj.completed_at.isoformat() if action_obj.completed_at else ""]
            )

        # Status history
        writer.writerow(["status_history", "count", defect.status_history.count()])
        for h in defect.status_history.all().order_by("changed_at"):
            writer.writerow(["status_change", "changed_at", h.changed_at.isoformat()])
            writer.writerow(["status_change", "from_status", h.from_status])
            writer.writerow(["status_change", "to_status", h.to_status])
            writer.writerow(["status_change", "changed_by", h.changed_by.username if h.changed_by else ""])
            writer.writerow(["status_change", "note", h.note])

        return response

    @action(detail=False, methods=["get"], url_path="export", renderer_classes=[CSVRenderer])
    def export_csv(self, request: Request) -> HttpResponse:
        """Export defects list as a CSV file (defects.csv).

        This is intended for the UI "Export CSV" button on the defects list page.
        The export respects the same filtering query parameters as the list endpoint
        (e.g. status=OPEN, severity=HIGH).

        Response headers:
        - Content-Type: text/csv
        - Content-Disposition: attachment; filename="defects.csv"
        """
        qs = self.get_queryset()

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="defects.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "id",
                "title",
                "area",
                "severity",
                "status",
                "priority",
                "reporter_name",
                "assigned_to_name",
                "occurred_at",
                "due_date",
                "created_at",
                "updated_at",
            ]
        )

        for d in qs.iterator():
            writer.writerow(
                [
                    d.id,
                    d.title,
                    d.area,
                    d.severity,
                    d.status,
                    d.priority,
                    d.reporter_name,
                    d.assigned_to_name,
                    d.occurred_at.isoformat() if d.occurred_at else "",
                    d.due_date.isoformat() if d.due_date else "",
                    d.created_at.isoformat() if d.created_at else "",
                    d.updated_at.isoformat() if d.updated_at else "",
                ]
            )

        return response


class RootCauseViewSet(viewsets.ModelViewSet):
    """
    Root cause CRUD + filtering.

    Filtering query params:
    - defect: defect id
    - status: NOT_STARTED|IN_PROGRESS|IDENTIFIED|APPROVED
    """

    serializer_class = RootCauseSerializer
    queryset = RootCause.objects.all().select_related("defect", "identified_by")

    def get_queryset(self) -> QuerySet[RootCause]:
        qs = super().get_queryset().order_by("-created_at")
        params = self.request.query_params
        defect_id = params.get("defect")
        status_val = params.get("status")
        if defect_id:
            qs = qs.filter(defect_id=defect_id)
        if status_val:
            qs = qs.filter(status=status_val)
        return qs

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request: Request, pk: str | None = None) -> Response:
        """Transition a root cause workflow status.

        Body:
        {
          "status": "<new_status>"
        }
        """
        root_cause = self.get_object()
        new_status = request.data.get("status")
        if not new_status:
            return Response({"status": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(
            root_cause, data={"status": new_status}, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # If RCA moved to IDENTIFIED/APPROVED, allow defect to move beyond OPEN (serializer enforces gating).
        return Response(serializer.data, status=status.HTTP_200_OK)


class CorrectiveActionViewSet(viewsets.ModelViewSet):
    """
    Corrective action CRUD + filtering.

    Filtering query params:
    - defect: defect id
    - root_cause: root cause id
    - status: OPEN|IN_PROGRESS|DONE|VERIFIED|CANCELED
    - owner: user id
    - due_from / due_to: YYYY-MM-DD
    """

    serializer_class = CorrectiveActionSerializer
    queryset = CorrectiveAction.objects.all().select_related("defect", "root_cause", "owner")

    def get_queryset(self) -> QuerySet[CorrectiveAction]:
        qs = super().get_queryset().order_by("-created_at")
        params = self.request.query_params

        defect_id = params.get("defect")
        root_cause_id = params.get("root_cause")
        status_val = params.get("status")
        owner_val = params.get("owner")
        due_from = params.get("due_from")
        due_to = params.get("due_to")

        if defect_id:
            qs = qs.filter(defect_id=defect_id)
        if root_cause_id:
            qs = qs.filter(root_cause_id=root_cause_id)
        if status_val:
            qs = qs.filter(status=status_val)
        if owner_val:
            qs = qs.filter(owner_id=owner_val)

        if due_from:
            d = parse_date(due_from)
            if d:
                qs = qs.filter(due_date__gte=d)
        if due_to:
            d = parse_date(due_to)
            if d:
                qs = qs.filter(due_date__lte=d)

        return qs

    @action(detail=True, methods=["post"], url_path="transition")
    def transition(self, request: Request, pk: str | None = None) -> Response:
        """Transition a corrective action status.

        Body:
        {
          "status": "<new_status>"
        }
        """
        action_obj = self.get_object()
        new_status = request.data.get("status")
        if not new_status:
            return Response({"status": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(
            action_obj, data={"status": new_status}, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)
