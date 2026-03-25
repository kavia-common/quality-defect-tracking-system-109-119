from __future__ import annotations

from django.db.models import QuerySet
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .models import CorrectiveAction, Defect, RootCause
from .serializers import CorrectiveActionSerializer, DefectSerializer, RootCauseSerializer


@api_view(["GET"])
def health(request: Request) -> Response:
    """Health check endpoint used by deployment and tests."""
    return Response({"message": "Server is up!"})


class DefectViewSet(viewsets.ModelViewSet):
    """
    Defect CRUD + filtering.

    Filtering query params:
    - status: Defect status (OPEN, INVESTIGATING, ...)
    - severity: LOW|MEDIUM|HIGH|CRITICAL
    - assignee: user id (integer)
    - reported_by: user id (integer)
    - created_from / created_to: ISO datetime or YYYY-MM-DD
    - occurred_from / occurred_to: ISO datetime or YYYY-MM-DD
    """

    serializer_class = DefectSerializer
    queryset = Defect.objects.all().select_related("assignee", "reported_by")

    def _parse_dt_or_date(self, value: str):
        dt = parse_datetime(value)
        if dt:
            return dt
        d = parse_date(value)
        if d:
            # treat date as start-of-day in UTC for >= and end-of-day for <= handled by caller
            return d
        return None

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

        created_from = params.get("created_from")
        created_to = params.get("created_to")
        if created_from:
            parsed = self._parse_dt_or_date(created_from)
            if parsed:
                qs = qs.filter(created_at__gte=parsed)
        if created_to:
            parsed = self._parse_dt_or_date(created_to)
            if parsed:
                qs = qs.filter(created_at__lte=parsed)

        occurred_from = params.get("occurred_from")
        occurred_to = params.get("occurred_to")
        if occurred_from:
            parsed = self._parse_dt_or_date(occurred_from)
            if parsed:
                qs = qs.filter(occurred_at__gte=parsed)
        if occurred_to:
            parsed = self._parse_dt_or_date(occurred_to)
            if parsed:
                qs = qs.filter(occurred_at__lte=parsed)

        return qs

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
        return Response(serializer.data, status=status.HTTP_200_OK)


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
