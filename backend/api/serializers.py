from __future__ import annotations

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
            "identified_by",
            "identified_by_detail",
            "identified_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

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
                    {"status": f"Invalid status transition {instance.status} -> {new_status}."}
                )

        # If status is IDENTIFIED/APPROVED, require summary.
        status = new_status
        summary = attrs.get("summary", getattr(instance, "summary", ""))
        if status in {RootCause.Status.IDENTIFIED, RootCause.Status.APPROVED}:
            if not (summary or "").strip():
                raise serializers.ValidationError(
                    {"summary": "Summary is required when root cause is identified/approved."}
                )
        return attrs

    def create(self, validated_data):
        # defect is mandatory on create (1:1 with Defect). We keep this requirement here
        # so PATCH/updates don't have to re-send defect_id.
        if not validated_data.get("defect"):
            raise serializers.ValidationError({"defect_id": "This field is required."})

        # Ensure identified_at auto-set when appropriate.
        if validated_data.get("status") in {RootCause.Status.IDENTIFIED, RootCause.Status.APPROVED}:
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
    owner = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    owner_detail = UserSlimSerializer(source="owner", read_only=True)

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
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        instance: CorrectiveAction | None = getattr(self, "instance", None)

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

        # If status done/verified, completed_at must be present (we will auto-set too).
        status = attrs.get("status", getattr(instance, "status", None))
        completed_at = attrs.get("completed_at", getattr(instance, "completed_at", None))
        if status in {CorrectiveAction.Status.DONE, CorrectiveAction.Status.VERIFIED} and not completed_at:
            # allow missing and auto-set in create/update
            pass
        return attrs

    def create(self, validated_data):
        status = validated_data.get("status")
        if status in {CorrectiveAction.Status.DONE, CorrectiveAction.Status.VERIFIED}:
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

    def validate(self, attrs):
        instance: Defect | None = getattr(self, "instance", None)
        if instance is not None and "status" in attrs:
            new_status = attrs["status"]
            if not instance.can_transition_to(new_status):
                raise serializers.ValidationError(
                    {"status": f"Invalid status transition {instance.status} -> {new_status}."}
                )
        title = attrs.get("title", getattr(instance, "title", ""))
        if title is not None and not title.strip():
            raise serializers.ValidationError({"title": "Title cannot be blank."})
        occurred_at = attrs.get("occurred_at", getattr(instance, "occurred_at", None))
        if occurred_at is not None and occurred_at > timezone.now() + timezone.timedelta(seconds=1):
            raise serializers.ValidationError({"occurred_at": "occurred_at cannot be in the future."})
        return attrs

    @transaction.atomic
    def update(self, instance, validated_data):
        # Optional: if a defect gets moved to ROOT_CAUSE_IDENTIFIED/ACTIONS_IN_PROGRESS etc,
        # we do not auto-create records; frontend/backoffice can create explicitly.
        return super().update(instance, validated_data)
