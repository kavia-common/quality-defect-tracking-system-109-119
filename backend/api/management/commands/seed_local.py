"""Seed minimal local data for development.

This command is intentionally minimal and safe to run multiple times.
It creates a superuser and a couple demo users if they do not already exist.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Seed minimal local data (admin + demo users)."

    # PUBLIC_INTERFACE
    def handle(self, *args, **options):
        """Run the local seed operation."""
        User = get_user_model()

        # Create admin user (superuser)
        admin_username = "admin"
        admin_email = "admin@example.com"
        admin_password = "admin123"

        admin, created = User.objects.get_or_create(
            username=admin_username,
            defaults={"email": admin_email, "is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_password(admin_password)
            admin.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created superuser '{admin_username}' (password: {admin_password})"
                )
            )
        else:
            self.stdout.write(f"Superuser '{admin_username}' already exists")

        # Create a couple demo users
        demo_users = [
            ("inspector", "inspector@example.com", "password123"),
            ("engineer", "engineer@example.com", "password123"),
        ]
        for username, email, password in demo_users:
            user, u_created = User.objects.get_or_create(
                username=username, defaults={"email": email}
            )
            if u_created:
                user.set_password(password)
                user.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created user '{username}' (password: {password})"
                    )
                )
            else:
                self.stdout.write(f"User '{username}' already exists")
