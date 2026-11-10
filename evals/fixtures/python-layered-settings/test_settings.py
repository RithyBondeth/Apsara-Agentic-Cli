import unittest
from copy import deepcopy

from settings.coerce import coerce_value
from settings.defaults import DEFAULTS
from settings.loader import merge_settings


EXPECTED_DEFAULTS = {
    "debug": False,
    "retries": 3,
    "regions": ["local"],
    "service": "api",
}


class CoerceValueTests(unittest.TestCase):
    def test_converts_supported_scalar_and_list_values(self):
        self.assertIs(coerce_value("true"), True)
        self.assertIs(coerce_value("OFF"), False)
        self.assertEqual(coerce_value(" 12 "), 12)
        self.assertEqual(coerce_value("us-east, eu-west"), ["us-east", "eu-west"])

    def test_preserves_unrecognized_and_non_string_values(self):
        self.assertEqual(coerce_value("api"), "api")
        self.assertEqual(coerce_value(7), 7)


class MergeSettingsTests(unittest.TestCase):
    def setUp(self):
        DEFAULTS.clear()
        DEFAULTS.update(deepcopy(EXPECTED_DEFAULTS))

    def tearDown(self):
        DEFAULTS.clear()
        DEFAULTS.update(deepcopy(EXPECTED_DEFAULTS))

    def test_precedence_and_type_coercion(self):
        merged = merge_settings(
            {"debug": "false", "retries": "5", "service": "worker"},
            {"APP_DEBUG": "true", "APP_REGIONS": "ap-south, eu-central"},
        )

        self.assertEqual(merged, {
            "debug": True,
            "retries": 5,
            "regions": ["ap-south", "eu-central"],
            "service": "worker",
        })

    def test_does_not_mutate_defaults_or_accept_unknown_environment_keys(self):
        merged = merge_settings({}, {"APP_UNKNOWN": "ignored"})
        merged["regions"].append("mutation")

        self.assertEqual(DEFAULTS, EXPECTED_DEFAULTS)
        self.assertNotIn("unknown", merged)


if __name__ == "__main__":
    unittest.main()
