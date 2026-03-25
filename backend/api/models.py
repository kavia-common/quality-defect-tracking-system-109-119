"""
Domain models for the Quality Defect Tracking System.

These models represent:
- Defect: the primary record (status/severity/assignee/timestamps)
- RootCause: workflow details linked 1:1 with a defect
- CorrectiveAction: one-to-many actions linked to a defect (optionally to a root cause)

We keep status transitions/validation close to the models so the same logic can be
reused by serializers and admin.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Defect(models.Model):
    """A quality defect record with workflow status and ownership."""

    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        INVESTIGATING = "INVESTIGATING", "Investigating"
        ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED", "Root cause identified"
        ACTIONS_IN_PROGRESS = "ACTIONS_IN_PROGRESS", "Actions in progress"
        VERIFIED = "VERIFIED", "Verified"
        CLOSED = "CLOSED", "Closed"

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    severity = models.CharField(
        max_length=16, choices=Severity.choices, default=Severity.MEDIUM
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.OPEN)

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_defects",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_defects",
    )

    occurred_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the defect occurred (optional).",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Target date for resolution (optional).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        """Model-level validation."""
        errors = {}
        if self.title is not None and not self.title.strip():
            errors["title"] = "Title cannot be blank."
        if (
            self.occurred_at is not None
            and self.occurred_at > timezone.now() + timezone.timedelta(seconds=1)
        ):
            errors["occurred_at"] = "occurred_at cannot be in the future."
        if errors:
            raise ValidationError(errors)

    # PUBLIC_INTERFACE
    def can_transition_to(self, new_status: str) -> bool:
        """Return True if the defect can transition from current status to new_status."""
        allowed = {
            self.Status.OPEN: {self.Status.INVESTIGATING, self.Status.CLOSED},
            self.Status.INVESTIGATING: {
                self.Status.ROOT_CAUSE_IDENTIFIED,
                self.Status.ACTIONS_IN_PROGRESS,
                self.Status.CLOSED,
            },
            self.Status.ROOT_CAUSE_IDENTIFIED: {
                self.Status.ACTIONS_IN_PROGRESS,
                self.Status.CLOSED,
            },
            self.Status.ACTIONS_IN_PROGRESS: {self.Status.VERIFIED, self.Status.CLOSED},
            self.Status.VERIFIED: {self.Status.CLOSED},
            self.Status.CLOSED: set(),
        }
        try:
            new = self.Status(new_status)
        except ValueError:
            return False
        return new in allowed.get(self.status, set())

    # PUBLIC_INTERFACE
    def transition_to(self, new_status: str, *, strict: bool = True) -> None:
        """Transition the defect to a new status, validating allowed transitions.

        Args:
            new_status: A Defect.Status value.
            strict: If True, raises ValidationError on invalid transition.
                    If False, does nothing on invalid transition.
        """
        if not self.can_transition_to(new_status):
            if strict:
                raise ValidationError(
                    {"status": f"Invalid status transition {self.status} -> {new_status}."}
                )
            return
        self.status = new_status

    def __str__(self) -> str:  # pragma: no cover
        return f"Defect#{self.pk} {self.title}"


class RootCause(models.Model):
    """Root cause analysis details for a defect (1:1)."""

    class Status(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", "Not started"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        IDENTIFIED = "IDENTIFIED", "Identified"
        APPROVED = "APPROVED", "Approved"

    defect = models.OneToOneField(
        Defect,
        on_delete=models.CASCADE,
        related_name="root_cause",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.NOT_STARTED
    )

    summary = models.TextField(blank=True, default="")
    analysis = models.TextField(blank=True, default="")
    identified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="root_causes_identified",
    )
    identified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        """Model-level validation."""
        errors = {}
        if self.status in {self.Status.IDENTIFIED, self.Status.APPROVED}:
            if not self.summary.strip():
                errors["summary"] = "Summary is required when root cause is identified/approved."
        if self.identified_at and self.identified_at > timezone.now() + timezone.timedelta(
            seconds=1
        ):
            errors["identified_at"] = "identified_at cannot be in the future."
        if errors:
            raise ValidationError(errors)

    # PUBLIC_INTERFACE
    def can_transition_to(self, new_status: str) -> bool:
        """Return True if the root cause status can transition to new_status."""
        allowed = {
            self.Status.NOT_STARTED: {self.Status.IN_PROGRESS, self.Status.IDENTIFIED},
            self.Status.IN_PROGRESS: {self.Status.IDENTIFIED},
            self.Status.IDENTIFIED: {self.Status.APPROVED},
            self.Status.APPROVED: set(),
        }
        try:
            new = self.Status(new_status)
        except ValueError:
            return False
        return new in allowed.get(self.status, set())

    # PUBLIC_INTERFACE
    def transition_to(self, new_status: str, *, strict: bool = True) -> None:
        """Transition root cause workflow status with validation."""
        if not self.can_transition_to(new_status):
            if strict:
                raise ValidationError(
                    {"status": f"Invalid status transition {self.status} -> {new_status}."}
                )
            return
        self.status = new_status
        if new_status in {self.Status.IDENTIFIED, self.Status.APPROVED} and not self.identified_at:
            self.identified_at = timezone.now()

    def __str__(self) -> str:  # pragma: no cover
        return f"RootCause(defect_id={self.defect_id})"


class CorrectiveAction(models.Model):
    """A corrective action for a defect (and optionally for its root cause)."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        DONE = "DONE", "Done"
        VERIFIED = "VERIFIED", "Verified"
        CANCELED = "CANCELED", "Canceled"

    defect = models.ForeignKey(
        Defect,
        on_delete=models.CASCADE,
        related_name="corrective_actions",
    )
    root_cause = models.ForeignKey(
        RootCause,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="corrective_actions",
    )

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_corrective_actions",
    )

    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        """Model-level validation."""
        errors = {}
        if self.title is not None and not self.title.strip():
            errors["title"] = "Title cannot be blank."

        # Ensure root_cause (if provided) belongs to same defect.
        if self.root_cause_id and self.defect_id and self.root_cause.defect_id != self.defect_id:
            errors["root_cause"] = "root_cause must belong to the same defect."

        if self.completed_at and self.completed_at > timezone.now() + timezone.timedelta(seconds=1):
            errors["completed_at"] = "completed_at cannot be in the future."

        if self.status in {self.Status.DONE, self.Status.VERIFIED} and not self.completed_at:
            # Allow serializers to set completed_at automatically, but enforce on model clean too.
            errors["completed_at"] = "completed_at is required when status is DONE/VERIFIED."

        if errors:
            raise ValidationError(errors)

    # PUBLIC_INTERFACE
    def can_transition_to(self, new_status: str) -> bool:
        """Return True if the action can transition from current status to new_status."""
        allowed = {
            self.Status.OPEN: {self.Status.IN_PROGRESS, self.Status.CANCELED},
            self.Status.IN_PROGRESS: {self.Status.DONE, self.Status.CANCELED},
            self.Status.DONE: {self.Status.VERIFIED},
            self.Status.VERIFIED: set(),
            self.Status.CANCELED: set(),
        }
        try:
            new = self.Status(new_status)
        except ValueError:
            return False
        return new in allowed.get(self.status, set())

    # PUBLIC_INTERFACE
    def transition_to(self, new_status: str, *, strict: bool = True) -> None:
        """Transition corrective action status with validation."""
        if not self.can_transition_to(new_status):
            if strict:
                raise ValidationError(
                    {"status": f"Invalid status transition {self.status} -> {new_status}."}
                )
            return
        self.status = new_status
        if new_status in {self.Status.DONE, self.Status.VERIFIED} and not self.completed_at:
            self.completed_at = timezone.now()

    def __str__(self) -> str:  # pragma: no cover
        return f"CorrectiveAction#{self.pk} {self.title}"
