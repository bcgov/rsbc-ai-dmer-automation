"""Live integration tests for categorizing every canonical condition field.

These tests call the real Azure OpenAI categorization path. Each test payload
simulates a slim extracted DMER containing a single canonical CONDITIONS field.

Usage: python -m unittest llm_normalization.tests.test_categorize_condition_fields_live
"""

import os
import sys
import unittest
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")


def _has_azure_openai_config() -> bool:
    return bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT")
        and os.environ.get("AZURE_OPENAI_API_KEY")
    )


def _slim(**fields: object) -> dict:
    return {"dmer": fields}


def _sample_value_for_type(field_type: str) -> object:
    if field_type == "bool":
        return True
    if field_type == "int":
        return 1
    if field_type == "float":
        return 1.0
    return "test value"


def _condition_field_payload(field_name: str, field_type: str) -> dict:
    if field_name == "details_of_condition":
        return _slim(details_of_condition="test medical condition")
    return _slim(
        **{
            field_name: _sample_value_for_type(field_type),
            "details_of_condition": "",
        }
    )


@unittest.skipUnless(
    _has_azure_openai_config(),
    "Azure OpenAI environment variables are required for live categorization tests.",
)
class CategorizeConditionFieldsLiveTests(unittest.TestCase):
    """Live category checks for every canonical field in CONDITIONS."""

    @classmethod
    def setUpClass(cls) -> None:
        from src.conditions import CATEGORY_CONDITIONS, CONDITIONS
        from src.processing import categorize_conditions

        cls.CONDITIONS = CONDITIONS
        cls.categorize_conditions = staticmethod(categorize_conditions)
        cls.expected_category_by_field = {
            field_name: category
            for category, category_conditions in CATEGORY_CONDITIONS.items()
            for field_name in category_conditions
        }

    def test_every_condition_field_has_category_mapping(self) -> None:
        missing_category = sorted(
            field_name
            for field_name in self.CONDITIONS
            if field_name not in self.expected_category_by_field
        )
        self.assertEqual([], missing_category)

    def test_each_condition_field_selects_its_category(self) -> None:
        for field_name, config in self.CONDITIONS.items():
            with self.subTest(condition=field_name):
                expected_category = self.expected_category_by_field[field_name]
                slim_json = _condition_field_payload(field_name, config["type"])
                categories = set(self.categorize_conditions(slim_json))
                self.assertIn(
                    expected_category,
                    categories,
                    "Expected category was not returned. "
                    f"Condition: {field_name}. "
                    f"Expected: {expected_category.value}. "
                    f"Actual: {sorted(category.value for category in categories)}. "
                    f"Payload: {slim_json}",
                )


if __name__ == "__main__":
    unittest.main()
