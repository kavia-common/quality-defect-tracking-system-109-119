from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import CorrectiveAction, Defect, RootCause

User = get_user_model()


class UserSlimSerializer(serializers.ModelSerializer):
    """Small embedded user representation."""

    class Meta:
        model = User
        fields = ["id", "username", "email"]


class RootCauseSerializer(serializers.ModelSerializer):
    defect_id = serializers.PrimaryKeyRelatedField(
        source="defect",
        queryset=Defect.objects.all(),
        write_only=True,
        required=False,
        help_text="Defect this root cause belongs to (1:1).",
    )
    # Backwards/alternate client support: some clients may send `defect` on create.
    # Keep it write-only to avoid changing read representation (read uses `defect` id).
    defect = serializers.PrimaryKeyRelatedField(
        queryset=Defect.objects.all(),
        required=False,
        write_only=True,
        help_text="Alternate write alias for defect_id. Prefer defect_id.",
    )

    identified_by = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )
    identified_by_detail = UserSlimSerializer(source="identified_by", read_only=True)

    class Meta:
        model = RootCause
        fields = [
            "id",
            "defect",
            "defect_id",
            "status",
            "summary",
            "analysis",
            # 5-Why fields (structured RCA)
            "why_1",
            "why_2",
            "why_3",
            "why_4",
            "why_5",
            "identified_by",
            "identified_by_detail",
            "identified_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _validate_5why(self, attrs, *, instance: RootCause | None) -> None:
        """Validate structured 5-Why fields.

        We require:
        - If any why_N is provided (non-empty), then why_1..why_5 must all be non-empty.
        This matches the UI expectation for a complete 5-Why chain once the user starts it.
        """
        why_fields = ["why_1", "why_2", "why_3", "why_4", "why_5"]
        values = []
        any_provided = False
        for f in why_fields:
            v = attrs.get(f, getattr(instance, f, ""))
            v = (v or "").strip()
            values.append(v)
            if v:
                any_provided = True

        if any_provided and not all(values):
            raise serializers.ValidationError(
                {
                    "why_1": "If using 5-Why analysis, all why_1..why_5 fields are required.",
                    "why_2": "If using 5-Why analysis, all why_1..why_5 fields are required.",
                    "why_3": "If using 5-Why analysis, all why_1..why_5 fields are required.",
                    "why_4": "If using 5-Why analysis, all why_1..why_5 fields are required.",
                    "why_5": "If using 5-Why analysis, all why_1..why_5 fields are required.",
                }
            )

    def validate(self, attrs):
        # Normalize `defect` alias into the canonical `defect` field used by the model.
        # If both are provided, defect_id takes precedence since it's the documented API.
        defect_obj = attrs.get("defect")
        if defect_obj is None and "defect_id" in attrs:
            defect_obj = attrs.get("defect_id")
            attrs["defect"] = defect_obj

        instance: RootCause | None = getattr(self, "instance", None)

        # Enforce 1:1 constraint at serializer level to avoid IntegrityError (500).
        if instance is None and defect_obj is not None:
            if RootCause.objects.filter(defect=defect_obj).exists():
                raise serializers.ValidationError(
                    {"defect_id": "Root cause already exists for this defect."}
                )

        # Enforce transition checks.
        new_status = attrs.get("status", getattr(instance, "status", None))
        if instance is not None and "status" in attrs:
            # Treat "same status" as a no-op to support clients that re-send status on save.
            if new_status != instance.status and not instance.can_transition_to(new_status):
                raise serializers.ValidationError(
                    {
                        "status": (
                            f"Invalid status transition {instance.status} -> {new_status}."
                        )
                    }
                )

        # If status is IDENTIFIED/APPROVED, require summary.
        status_val = new_status
        summary = attrs.get("summary", getattr(instance, "summary", ""))
        if status_val in {RootCause.Status.IDENTIFIED, RootCause.Status.APPROVED}:
            if not (summary or "").strip():
                raise serializers.ValidationError(
                    {"summary": "Summary is required when root cause is identified/approved."}
                )

        # Validate 5-Why chain if present.
        self._validate_5why(attrs, instance=instance)

        return attrs

    def create(self, validated_data):
        # defect is mandatory on create (1:1 with Defect). We keep this requirement here
        # so PATCH/updates don't have to re-send defect_id.
        if not validated_data.get("defect"):
            raise serializers.ValidationError({"defect_id": "This field is required."})

        # Ensure identified_at auto-set when appropriate.
        if validated_data.get("status") in {
            RootCause.Status.IDENTIFIED,
            RootCause.Status.APPROVED,
        }:
            validated_data.setdefault("identified_at", timezone.now())
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "status" in validated_data and validated_data["status"] in {
            RootCause.Status.IDENTIFIED,
            RootCause.Status.APPROVED,
        }:
            validated_data.setdefault("identified_at", instance.identified_at or timezone.now())
        return super().update(instance, validated_data)


class CorrectiveActionSerializer(serializers.ModelSerializer):
    defect = serializers.PrimaryKeyRelatedField(queryset=Defect.objects.all())
    root_cause = serializers.PrimaryKeyRelatedField(
        queryset=RootCause.objects.all(), required=False, allow_null=True
    )
    owner = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=True)
    owner_detail = UserSlimSerializer(source="owner", read_only=True)

    is_overdue = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()

    class Meta:
        model = CorrectiveAction
        fields = [
            "id",
            "defect",
            "root_cause",
            "title",
            "description",
            "status",
            "owner",
            "owner_detail",
            "due_date",
            "completed_at",
            # computed for UI alerts
            "is_overdue",
            "days_overdue",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _compute_overdue(self, obj: CorrectiveAction) -> tuple[bool, int]:
        if not obj.due_date:
            return False, 0
        if obj.completed_at is not None or obj.status in {
            CorrectiveAction.Status.DONE,
            CorrectiveAction.Status.VERIFIED,
            CorrectiveAction.Status.CANCELED,
        }:
            return False, 0
        today: date = timezone.now().date()
        if obj.due_date < today:
            days = (today - obj.due_date).days
            return True, max(days, 0)
        return False, 0

    # PUBLIC_INTERFACE
    def get_is_overdue(self, obj: CorrectiveAction) -> bool:
        """Return True if action is overdue (due_date passed and not completed/canceled)."""
        return self._compute_overdue(obj)[0]

    # PUBLIC_INTERFACE
    def get_days_overdue(self, obj: CorrectiveAction) -> int:
        """Return the number of days overdue (0 if not overdue)."""
        return self._compute_overdue(obj)[1]

    def validate(self, attrs):
        instance: CorrectiveAction | None = getattr(self, "instance", None)

        # Required fields (acceptance criteria): description, owner, due_date, status.
        # title already required by model/serializer field.
        description = attrs.get("description", getattr(instance, "description", ""))
        if not (description or "").strip():
            raise serializers.ValidationError({"description": "Description is required."})

        owner = attrs.get("owner", getattr(instance, "owner", None))
        if owner is None:
            raise serializers.ValidationError({"owner": "Owner is required."})

        due_date = attrs.get("due_date", getattr(instance, "due_date", None))
        if due_date is None:
            raise serializers.ValidationError({"due_date": "Due date is required."})

        # Validate status transition on update.
        if instance is not None and "status" in attrs:
            if not instance.can_transition_to(attrs["status"]):
                raise serializers.ValidationError(
                    {"status": f"Invalid status transition {instance.status} -> {attrs['status']}."}
                )

        # Ensure root cause belongs to defect if both present.
        defect = attrs.get("defect", getattr(instance, "defect", None))
        root_cause = attrs.get("root_cause", getattr(instance, "root_cause", None))
        if root_cause is not None and defect is not None and root_cause.defect_id != defect.id:
            raise serializers.ValidationError(
                {"root_cause": "root_cause must belong to the same defect."}
            )

        # If status done/verified, completed_at may be omitted and auto-set in create/update.
        return attrs

    def create(self, validated_data):
        status_val = validated_data.get("status")
        if status_val in {CorrectiveAction.Status.DONE, CorrectiveAction.Status.VERIFIED}:
            validated_data.setdefault("completed_at", timezone.now())
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "status" in validated_data and validated_data["status"] in {
            CorrectiveAction.Status.DONE,
            CorrectiveAction.Status.VERIFIED,
        }:
            validated_data.setdefault("completed_at", instance.completed_at or timezone.now())
        return super().update(instance, validated_data)


class DefectSerializer(serializers.ModelSerializer):
    assignee = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    assignee_detail = UserSlimSerializer(source="assignee", read_only=True)

    reported_by = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    reported_by_detail = UserSlimSerializer(source="reported_by", read_only=True)

    root_cause = RootCauseSerializer(read_only=True)
    corrective_actions = CorrectiveActionSerializer(many=True, read_only=True)

    class Meta:
        model = Defect
        fields = [
            "id",
            "title",
            "description",
            "severity",
            "status",
            # UI triage/display fields
            "priority",
            "area",
            "tags",
            "reporter_name",
            "assigned_to_name",
            # user-based fields (existing)
            "assignee",
            "assignee_detail",
            "reported_by",
            "reported_by_detail",
            "occurred_at",
            "due_date",
            "root_cause",
            "corrective_actions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _validate_root_cause_gating(self, instance: Defect, new_status: str) -> None:
        """Enforce: root cause must be completed/approved before moving beyond OPEN."""
        if instance.status == Defect.Status.OPEN and new_status != Defect.Status.OPEN:
            # root_cause row should exist (migration backfilled), but handle missing safely.
            rc = getattr(instance, "root_cause", None)
            if rc is None or rc.status not in {RootCause.Status.IDENTIFIED, RootCause.Status.APPROVED}:
                raise serializers.ValidationError(
                    {
                        "status": (
                            'Root cause must be identified/approved before moving defect beyond "Open".'
                        )
                    }
                )

    def _validate_actions_gate_for_close(self, instance: Defect, new_status: str) -> None:
        """Enforce: closing requires at least one corrective action and none overdue/open."""
        if new_status != Defect.Status.CLOSED:
            return

        actions_qs = instance.corrective_actions.all()
        if not actions_qs.exists():
            raise serializers.ValidationError(
                {"status": "At least one corrective action is required before closing a defect."}
            )

        # Require all actions to be DONE/VERIFIED (not open/in progress/canceled).
        not_done = actions_qs.exclude(status__in=[CorrectiveAction.Status.DONE, CorrectiveAction.Status.VERIFIED])
        if not_done.exists():
            raise serializers.ValidationError(
                {"status": "All corrective actions must be completed (DONE/VERIFIED) before closing."}
            )

    def validate(self, attrs):
        instance: Defect | None = getattr(self, "instance", None)

        # Enforce new simplified transition flow in addition to model-level allowed transitions.
        # Acceptance criteria: Open → Investigating → Actions In Progress → Closed
        if instance is not None and "status" in attrs:
            new_status = attrs["status"]

            allowed_flow = {
                Defect.Status.OPEN: {Defect.Status.INVESTIGATING, Defect.Status.CLOSED},
                Defect.Status.INVESTIGATING: {Defect.Status.ACTIONS_IN_PROGRESS, Defect.Status.CLOSED},
                Defect.Status.ACTIONS_IN_PROGRESS: {Defect.Status.CLOSED},
                Defect.Status.CLOSED: set(),
                # Legacy states: allow moving into the new flow, but disallow moving out.
                Defect.Status.ROOT_CAUSE_IDENTIFIED: {Defect.Status.ACTIONS_IN_PROGRESS, Defect.Status.CLOSED},
                Defect.Status.VERIFIED: {Defect.Status.CLOSED},
            }

            if new_status != instance.status and new_status not in allowed_flow.get(instance.status, set()):
                raise serializers.ValidationError(
                    {"status": f"Invalid status transition {instance.status} -> {new_status}."}
                )

            # Root cause gating (must have RCA identified/approved before moving beyond OPEN).
            self._validate_root_cause_gating(instance, new_status)

            # Closing gating.
            self._validate_actions_gate_for_close(instance, new_status)

        title = attrs.get("title", getattr(instance, "title", ""))
        if title is not None and not title.strip():
            raise serializers.ValidationError({"title": "Title cannot be blank."})

        occurred_at = attrs.get("occurred_at", getattr(instance, "occurred_at", None))
        if occurred_at is not None and occurred_at > timezone.now() + timezone.timedelta(seconds=1):
            raise serializers.ValidationError({"occurred_at": "occurred_at cannot be in the future."})

        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        return super().update(instance, validated_data)
