"""Live integration tests for DMER category classification.

These tests call the real Azure OpenAI categorization path. The payloads are
slim JSON snippets that simulate the output of extract_llm_fields(), not full
DMER source JSON files.

Usage: python -m unittest llm_normalization.tests.test_categorize_conditions_live
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


@unittest.skipUnless(
    _has_azure_openai_config(),
    "Azure OpenAI environment variables are required for live categorization tests.",
)
class CategorizeConditionsLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from src.conditions import ConditionCategory
        from src.processing import categorize_conditions

        cls.ConditionCategory = ConditionCategory
        cls.categorize_conditions = staticmethod(categorize_conditions)

    def assert_category_selected(
        self,
        fixture_name: str,
        slim_json: dict,
        expected_category_name: str,
    ) -> None:
        expected_category = self.ConditionCategory(expected_category_name)
        categories = set(self.categorize_conditions(slim_json))
        self.assertIn(
            expected_category,
            categories,
            "Expected category was not returned. "
            f"Fixture: {fixture_name}. "
            f"Expected: {expected_category.value}. "
            f"Actual: {sorted(category.value for category in categories)}. "
            f"Payload: {slim_json}",
        )

    def test_dmer_20_60_one_eye_worse(self) -> None:
        self.assert_category_selected(
            "dmer_20_60_one_eye_worse.json",
            _slim(
                **{
                    "visual_acuity.corrected_left": "20/60",
                    "visual_acuity.corrected_right": "20/40",
                    "visual_acuity.corrected_both": "20/50",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )

    def test_dmer_20_60_uncorrected(self) -> None:
        self.assert_category_selected(
            "dmer_20_60_uncorrected.json",
            _slim(
                **{
                    "visual_acuity.uncorrected_both": "20/60",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )

    def test_dmer_20_80_corrected_both(self) -> None:
        self.assert_category_selected(
            "dmer_20_80_corrected_both.json",
            _slim(
                **{
                    "visual_acuity.corrected_both": "20/80",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )

    def test_dmer_20_80_corrected_one_eye(self) -> None:
        self.assert_category_selected(
            "dmer_20_80_corrected_one_eye.json",
            _slim(
                **{
                    "visual_acuity.corrected_left": "20/60",
                    "visual_acuity.corrected_right": "20/80",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )

    def test_dmer_ahi_in_details(self) -> None:
        self.assert_category_selected(
            "dmer_ahi_in_details.json",
            _slim(details_of_condition="patient has ahi score of 11"),
            "sleep",
        )

    def test_dmer_aortic_aneurysm(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_aneurysm.json",
            _slim(
                **{
                    "pvd.aneurysm": True,
                    "pvd.aneurysm_site": "abdominal aorta",
                    "pvd.aneurysm_size": "6.9",
                    "details_of_condition": "",
                }
            ),
            "pvd",
        )

    def test_dmer_aortic_aneurysm_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_aneurysm_concerns.json",
            _slim(details_of_condition="patient has an abdominal aortic aneurysm of size 6.9 cm, imminent rupture"),
            "pvd",
        )

    def test_dmer_aortic_aneurysm_repaired(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_aneurysm_repaired.json",
            _slim(details_of_condition="patient has an abdominal aortic aneurysm, clipped"),
            "pvd",
        )

    def test_dmer_aortic_aneurysm_written(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_aneurysm_written.json",
            _slim(details_of_condition="patient has an abdominal aortic aneurysm of size 6.9 cm"),
            "pvd",
        )

    def test_dmer_aortic_dissection(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_dissection.json",
            _slim(details_of_condition="patient has an aortic dissection"),
            "pvd",
        )

    def test_dmer_aortic_dissection_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_aortic_dissection_concerns.json",
            _slim(details_of_condition="patient has an aortic dissection with surgical complications"),
            "pvd",
        )

    def test_dmer_arrythmia(self) -> None:
        self.assert_category_selected(
            "dmer_arrythmia.json",
            _slim(details_of_condition="patient has arrythmia"),
            "cardiovascular",
        )

    def test_dmer_arrythmia_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_arrythmia_related_term.json",
            _slim(details_of_condition="patient has tachycardia"),
            "cardiovascular",
        )

    def test_dmer_arrythmia_related_term_2(self) -> None:
        self.assert_category_selected(
            "dmer_arrythmia_related_term_2.json",
            _slim(details_of_condition="patient has rapid heart rate"),
            "cardiovascular",
        )

    def test_dmer_cad(self) -> None:
        self.assert_category_selected(
            "dmer_cad.json",
            _slim(details_of_condition="patient has coronary artery disease"),
            "cardiovascular",
        )

    def test_dmer_cad_has_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_cad_has_concerns.json",
            _slim(details_of_condition="patient has coronary artery disease and may sometimes become confused due to loss of blood flow"),
            "cardiovascular",
        )

    def test_dmer_cad_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_cad_related_term.json",
            _slim(details_of_condition="patient has an angina"),
            "cardiovascular",
        )

    def test_dmer_carotid_stenosis(self) -> None:
        self.assert_category_selected(
            "dmer_carotid_stenosis.json",
            _slim(details_of_condition="patient has a carotid stenosis, may sometimes lose consciousness"),
            "pvd",
        )

    def test_dmer_concussion(self) -> None:
        self.assert_category_selected(
            "dmer_concussion.json",
            _slim(details_of_condition="patient has tbi"),
            "traumatic_brain_injury",
        )

    def test_dmer_cp(self) -> None:
        self.assert_category_selected(
            "dmer_cp.json",
            _slim(details_of_condition="patient has a cerebral palsy"),
            "cns",
        )

    def test_dmer_daytime_sleepiness(self) -> None:
        self.assert_category_selected(
            "dmer_daytime_sleepiness.json",
            _slim(details_of_condition="patient has sleep apnea with daytime sleepiness"),
            "sleep",
        )    

    def test_dmer_diabetes_checkbox(self) -> None:
        self.assert_category_selected(
            "dmer_diabetes_checkbox.json",
            _slim(**{"endocrine.diabetes": True, "details_of_condition": ""}),
            "endocrine",
        )

    def test_dmer_diabetes_non_compliant(self) -> None:
        self.assert_category_selected(
            "dmer_diabetes_non_compliant.json",
            _slim(details_of_condition="patient has diabetes and is non compliant"),
            "endocrine",
        )

    def test_dmer_diabetes_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_diabetes_related_term.json",
            _slim(details_of_condition="patient has iddm"),
            "endocrine",
        )

    def test_dmer_diabetes_written(self) -> None:
        self.assert_category_selected(
            "dmer_diabetes_written.json",
            _slim(details_of_condition="patient has diabetes"),
            "endocrine",
        )

    def test_dmer_eczema(self) -> None:
        self.assert_category_selected(
            "dmer_eczema.json",
            _slim(details_of_condition="patient has eczema"),
            "general",
        )

    def test_dmer_eye_nerve_palsy(self) -> None:
        self.assert_category_selected(
            "dmer_eye_nerve_palsy.json",
            _slim(details_of_condition="patient has post-CVA eye nerve palsy"),
            "vision",
        )

    def test_dmer_field_and_acuity(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity.json",
            _slim(**{"visual_field.meet_criteria_for_licence_class_yes": True, "details_of_condition": ""}),
            "visual_field",
        )

    def test_dmer_field_and_acuity_not_met(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity_not_met.json",
            _slim(details_of_condition="patient's visual field and acuity meet standard for license class"),
            "visual_field",
        )

    def test_dmer_field_and_acuity_not_met2(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity_not_met2.json",
            _slim(**{"visual_field.abnormal": True, "details_of_condition": ""}),
            "visual_field",
        )

    def test_dmer_field_and_acuity_not_met_written(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity_not_met_written.json",
            _slim(details_of_condition="patient's visual field and acuity do not meet standard for license class"),
            "visual_field",
        )

    def test_dmer_field_and_acuity_not_met_written_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity_not_met_written_concerns.json",
            _slim(details_of_condition="patient's visual field and acuity do not meet standard for license class"),
            "visual_field",
        )

    def test_dmer_field_and_acuity_written(self) -> None:
        self.assert_category_selected(
            "dmer_field_and_acuity_written.json",
            _slim(details_of_condition="patient's visual field and acuity meet standard for license class"),
            "visual_field",
        )

    def test_dmer_general_debility(self) -> None:
        self.assert_category_selected(
            "dmer_general_debility.json",
            _slim(details_of_condition="patient has general debility"),
            "general",
        )

    def test_dmer_general_debility_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_general_debility_related_term.json",
            _slim(details_of_condition="patient is very frail and weak"),
            "general",
        )

    def test_dmer_general_debility_written_field(self) -> None:
        self.assert_category_selected(
            "dmer_general_debility_written_field.json",
            _slim(**{"general.other": "reduced rxn time", "details_of_condition": ""}),
            "general",
        )

    def test_dmer_iq(self) -> None:
        self.assert_category_selected(
            "dmer_iq.json",
            _slim(details_of_condition="patient has low iq"),
            "psychiatric",
        )

    def test_dmer_lvef_score_in_details(self) -> None:
        self.assert_category_selected(
            "dmer_lvef_score_in_details.json",
            _slim(details_of_condition="patient has lvef score 34%"),
            "cardiovascular",
        )

    def test_dmer_monocular(self) -> None:
        self.assert_category_selected(
            "dmer_monocular.json",
            _slim(details_of_condition="patient lacks depth perception"),
            "vision",
        )

    def test_dmer_narcolepsy_controlled(self) -> None:
        self.assert_category_selected(
            "dmer_narcolepsy_controlled.json",
            _slim(details_of_condition="patient has cataplexy and it is under control with medication"),
            "sleep",
        )

    def test_dmer_narcolepsy_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_narcolepsy_related_term.json",
            _slim(details_of_condition="patient has cataplexy"),
            "sleep",
        )

    def test_dmer_narcolepsy_related_term2(self) -> None:
        self.assert_category_selected(
            "dmer_narcolepsy_related_term2.json",
            _slim(details_of_condition="patient has sleep attacks"),
            "sleep",
        )

    def test_dmer_narcolepsy_uncontrolled(self) -> None:
        self.assert_category_selected(
            "dmer_narcolepsy_uncontrolled.json",
            _slim(details_of_condition="patient has uncontrolled cataplexy"),
            "sleep",
        )

    def test_dmer_no_daytime_sleepiness(self) -> None:
        self.assert_category_selected(
            "dmer_no_daytime_sleepiness.json",
            _slim(details_of_condition="patient has sleep apnea without any daytime sleepiness"),
            "sleep",
        )

    def test_dmer_plegia(self) -> None:
        self.assert_category_selected(
            "dmer_plegia.json",
            _slim(details_of_condition="patient has plegia"),
            "cns",
        )

    def test_dmer_plegia2(self) -> None:
        self.assert_category_selected(
            "dmer_plegia2.json",
            _slim(details_of_condition="patient has plegia"),
            "cns",
        )

    def test_dmer_psychosis(self) -> None:
        self.assert_category_selected(
            "dmer_psychosis.json",
            _slim(details_of_condition="patient is psychotic, condition is controlled with meds"),
            "psychiatric",
        )

    def test_dmer_psychosis_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_psychosis_concerns.json",
            _slim(details_of_condition="patient can be psychotic, sometimes judgement can be impaired"),
            "psychiatric",
        )

    def test_dmer_severe_depression(self) -> None:
        self.assert_category_selected(
            "dmer_severe_depression.json",
            _slim(details_of_condition="patient has severe depression"),
            "psychiatric",
        )

    def test_dmer_strabismus(self) -> None:
        self.assert_category_selected(
            "dmer_strabismus.json",
            _slim(details_of_condition="patient has strabismus"),
            "vision",
        )

    def test_dmer_strabismus_concerns(self) -> None:
        self.assert_category_selected(
            "dmer_strabismus_concerns.json",
            _slim(details_of_condition="patient has strabismus, has trouble seeing straight"),
            "vision",
        )

    def test_dmer_strabismus_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_strabismus_related_term.json",
            _slim(details_of_condition="patient has hypertropia"),
            "vision",
        )

    def test_dmer_tupr(self) -> None:
        self.assert_category_selected(
            "dmer_tupr.json",
            _slim(details_of_condition="patient has a transurethral prostate resection"),
            "general",
        )

    def test_dmer_vertigo(self) -> None:
        self.assert_category_selected(
            "dmer_vertigo.json",
            _slim(details_of_condition="patient has vertigo"),
            "vestibular",
        )

    def test_dmer_vertigo_related_term(self) -> None:
        self.assert_category_selected(
            "dmer_vertigo_related_term.json",
            _slim(details_of_condition="patient has Meniere's disease, can sometimes affect their driving"),
            "vestibular",
        )

    def test_dmer_worse_than_20_100(self) -> None:
        self.assert_category_selected(
            "dmer_worse_than_20_100.json",
            _slim(
                **{
                    "visual_acuity.corrected_left": "20/120",
                    "visual_acuity.corrected_right": "20/60",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )

    def test_dmer_worse_than_20_80(self) -> None:
        self.assert_category_selected(
            "dmer_worse_than_20_80.json",
            _slim(
                **{
                    "visual_acuity.corrected_left": "20/100",
                    "details_of_condition": "",
                }
            ),
            "visual_acuity",
        )


if __name__ == "__main__":
    unittest.main()
