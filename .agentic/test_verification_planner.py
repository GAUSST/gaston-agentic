"""Regression tests for language-aware verification path inference."""
import unittest

from verification_planner import infer_path_areas, select_checks


class VerificationPlannerPathInferenceTests(unittest.TestCase):
    def test_flutter_toast_controller_does_not_require_api_integration(self):
        areas, ripple = infer_path_areas(["lib/kp_toast_controller.dart"])

        self.assertEqual(areas, {"mobile"})
        self.assertEqual(ripple, set())

        steps = [
            {"name": "api_integration"},
            {"name": "flutter_analyze"},
            {"name": "flutter_tests"},
            {"name": "mobile_e2e"},
            {"name": "backend_verify"},
        ]
        result = select_checks({}, steps, ["lib/kp_toast_controller.dart"])

        self.assertEqual(result["areas"], ["mobile"])
        self.assertNotIn("api_integration", result["selected"])
        self.assertIn("flutter_analyze", result["selected"])
        self.assertIn("flutter_tests", result["selected"])
        self.assertNotIn("api_integration", result["missing"])
        self.assertEqual(result["missing"], [])

    def test_flutter_controller_directory_remains_mobile_only(self):
        areas, ripple = infer_path_areas(
            ["lib/controllers/kp_toast_controller.dart"]
        )

        self.assertEqual(areas, {"mobile"})
        self.assertEqual(ripple, set())

    def test_java_controller_still_requires_api_integration(self):
        path = "backend/src/main/java/com/example/PaymentController.java"
        areas, ripple = infer_path_areas([path])

        self.assertEqual(areas, {"backend", "api"})
        self.assertEqual(ripple, {"flutter_tests", "web_e2e"})

        steps = [
            {"name": "api_integration"},
            {"name": "backend_verify"},
            {"name": "flutter_tests"},
            {"name": "web_e2e"},
        ]
        result = select_checks({}, steps, [path])

        self.assertIn("api_integration", result["selected"])
        self.assertNotIn("api_integration", result["missing"])

    def test_backend_typescript_controller_still_requires_api_integration(self):
        areas, ripple = infer_path_areas(
            ["backend/src/controllers/payment_controller.ts"]
        )

        self.assertEqual(areas, {"backend", "api"})
        self.assertEqual(ripple, {"flutter_tests", "web_e2e"})

    def test_backend_python_controller_still_requires_api_integration(self):
        areas, ripple = infer_path_areas(
            ["backend/controllers/payment_controller.py"]
        )

        self.assertEqual(areas, {"backend", "api"})
        self.assertEqual(ripple, {"flutter_tests", "web_e2e"})


if __name__ == "__main__":
    unittest.main()
