from django.urls import reverse
from rest_framework.test import APITestCase


class HealthTests(APITestCase):
    def test_health(self):
        url = reverse("Health")  # Make sure the URL is named
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"message": "Server is up!"})


class DefectCreateTests(APITestCase):
    def test_create_defect_succeeds(self):
        """
        Regression test for 500s on POST /api/defects/ caused by schema drift.

        Ensures the create flow works and that the UI triage/display fields are accepted
        and returned.
        """
        url = reverse("defect-list")
        payload = {
            "title": "New defect from test",
            "description": "Created via API test",
            "severity": "MEDIUM",
            "status": "OPEN",
            "priority": "P2",
            "area": "Packaging",
            "tags": ["ui", "regression"],
            "reporter_name": "Alice",
            "assigned_to_name": "Bob",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["title"], payload["title"])
        self.assertEqual(response.data["priority"], payload["priority"])
        self.assertEqual(response.data["area"], payload["area"])
        self.assertEqual(response.data["tags"], payload["tags"])
        self.assertEqual(response.data["reporter_name"], payload["reporter_name"])
        self.assertEqual(response.data["assigned_to_name"], payload["assigned_to_name"])
