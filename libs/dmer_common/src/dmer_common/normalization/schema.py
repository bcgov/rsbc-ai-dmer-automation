"""DMER normalization condition schema.

Canonical list of every DMER condition the Normalize activity evaluates,
together with type/keyword metadata, the category groupings used to batch
the two-stage LLM analysis (see ``pipeline.py``), and the BC Guide section
mapping used by the guide-matching meta field.

Ported from the ``llm_normalization`` POC (see
docs/development/stages/04-activity-normalize.md).
"""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# CONDITIONS — single source of truth for every DMER condition.
#
#   "type"     – "bool" (default) | "str" | "int" | "float"
#   "description" – keywords or instructions describing what to look for in the DMER.
# ---------------------------------------------------------------------------
CONDITIONS: dict[str, dict] = {
    # --- Top-level ---
    "current_licence_class": {"type": "str", "description": ""},
    "blood_pressure": {"type": "str", "description": ""},
    "restrictions": {"type": "str", "description": ""},
    "medical_examination_date": {"type": "str", "description": ""},
    # --- Vision ---
    "vision.acuity_loss_cause": {"type": "str", "description": ""},
    "vision.field_defect_cause": {"type": "str", "description": ""},
    "vision.eye_nerve_palsy": {
        "type": "bool",
        "description": "post-CVA, cranial nerve, diabetic eye nerve palsy",
    },
    "vision.eye_nerve_palsy_has_concerns": {
        "type": "bool",
        "description": "check details of condition for any concerns specific to eye nerve palsy",
    },
    "vision.diplopia": {"type": "bool", "description": "diplopia, double vision"},
    "vision.diplopia_has_concerns": {
        "type": "bool",
        "description": "check details of condition for any concerns specific to diplopia",
    },
    "vision.strabismus": {
        "type": "bool",
        "description": "hypertropia, hypotropia, esotropia, exotropia, eye misalignment",
    },
    "vision.strabismus_has_concerns": {
        "type": "bool",
        "description": "check details of condition to for any concerns specific to strabismus",
    },
    "vision.monocular": {"type": "bool", "description": "monocular, monocular vision, loss of depth perception, lack of depth perception, blind in one eye"},
    "vision.monocular_has_concerns": {"type": "bool", "description": ""},
    "vision.monocular_date": {"type": "str", "description": ""},
    "vision.color_blindness": {"type": "bool", "description": ""},
    "vision.retinopathy": {"type": "bool", "description": ""},
    "vision.retinal_detachment": {"type": "bool", "description": ""},
    "vision.crao": {"type": "bool", "description": "central retinal artery"},
    "vision.crvo": {"type": "bool", "description": "central retinal vein"},
    "vision.cataracts": {"type": "bool", "description": "cataract, cataracts"},
    "vision.cataracts_had_surgery": {
        "type": "bool",
        "description": "cataract extraction, cataract surgery, cataract removal, cataract implant, IOL",
    },
    "vision.progressive_eye_condition": {
        "type": "bool",
        "description": "macular degeneration, AMD, glaucoma, cataracts, retinopathy, diabetic retinopathy, or another progressive eye disease. Also set deterministically when vision.cataracts or vision.retinopathy is true",
    },
    "vision.other": {"type": "str", "description": ""},
    # --- Cognition ---
    "cns.cognitiveimpairment": {
        "type": "bool",
        "description": "only look for keywords 'cognitive' or specific diagnosis",
    },
    "cns.dementia": {"type": "bool", "description": ""},
    "cns.dementia_details": {"type": "str", "description": ""},
    "cns.alzheimers": {"type": "bool", "description": ""},
    "cns.mmse_score": {"type": "int", "description": ""},
    "cns.moca_score": {"type": "int", "description": ""},
    "cns.simard_score": {"type": "int", "description": ""},
    "cns.gds_level_score": {"type": "int", "description": ""},
    "cns.trails_a_seconds": {"type": "int", "description": ""},
    "cns.trails_b_seconds": {"type": "int", "description": ""},
    "cns.cognitiveimpairment_is_severe": {"type": "bool", "description": ""},
    "cns.cognitiveimpairment_is_mild_or_moderate": {
        "type": "bool",
        "description": "physician describes the cognitive impairment/decline as mild or moderate",
    },
    "cns.cognitiveimpairment_has_concerns": {"type": "bool", "description": ""},
    "cns.dementia_has_concern": {
        "type": "bool",
        "description": "physician expresses concern about the dementia's impact on driving",
    },
    "cns.alzheimers_has_concern": {
        "type": "bool",
        "description": "physician expresses concern about the Alzheimer's disease's impact on driving",
    },
    "cns.cognitiveimpairment_s7_15_suspected": {"type": "bool", "description": ""},
    "cns.significant_head_injury_date": {"type": "str", "description": ""},
    "cns.other": {"type": "str", "description": ""},
    # --- Visual Acuity ---
    "visual_acuity.corrected_left": {"type": "str", "description": ""},
    "visual_acuity.corrected_right": {"type": "str", "description": ""},
    "visual_acuity.corrected_both": {"type": "str", "description": ""},
    "visual_acuity.uncorrected_left": {"type": "str", "description": ""},
    "visual_acuity.uncorrected_right": {"type": "str", "description": ""},
    "visual_acuity.uncorrected_both": {"type": "str", "description": ""},
    # --- Visual Field ---
    "visual_field.normal": {"type": "bool", "description": ""},
    "visual_field.abnormal": {"type": "bool", "description": ""},
    "visual_field.abnormal_has_concerns": {"type": "bool", "description": ""},
    "visual_field.meet_criteria_for_licence_class_yes": {
        "type": "bool",
        "description": "the YES checkbox for whether the visual field meets the criteria for the licence class; set only from that checkbox",
    },
    # --- Opinion ---
    "opinion.yes": {"type": "bool", "description": ""},
    "opinion.no": {"type": "bool", "description": ""},
    "opinion.maybe": {"type": "bool", "description": ""},
    "opinion.maybe_followup_years": {
        "type": "int",
        "description": "if no followup years given, set to null",
    },
    # --- Details ---
    "details_of_condition": {"type": "str", "description": ""},
    # --- Recommendations ---
    "recommendations.road_test_to_assess": {"type": "bool", "description": "road test"},
    "recommendations.road_test_to_assess_details": {"type": "str", "description": ""},
    "recommendations.rationale_for_road_test": {"type": "str", "description": ""},
    "recommendations.restrictions_reason_and_type": {"type": "str", "description": ""},
    "recommendations.specialist_consult_type": {"type": "str", "description": ""},
    # --- Relationship ---
    "relationship_with_patient.family_physician_years": {
        "type": "str",
        "description": "",
    },
    # --- Cerebrovascular ---
    "cerebrovascular.cerebral_aneurysm": {
        "type": "bool",
        "description": "cerebral aneurysm",
    },
    "cerebrovascular.cerebral_aneurysm_requires_repair": {
        "type": "bool",
        "description": "",
    },
    "cerebrovascular.cerebral_aneurysm_repaired": {"type": "bool", "description": ""},
    "cerebrovascular.cerebral_aneurysm_residual_neuro_deficit": {
        "type": "bool",
        "description": "",
    },
    "cerebrovascular.cerebral_aneurysm_date": {"type": "str", "description": ""},
    "cerebrovascular.cva": {"type": "bool", "description": "CVA, stroke"},
    "cerebrovascular.tia": {"type": "bool", "description": "TIA, transient ischemic"},
    "cerebrovascular.cva_tia_residual_neuro_deficit": {
        "type": "bool",
        "description": "",
    },
    "cerebrovascular.cva_tia_date": {"type": "str", "description": ""},
    "cerebrovascular.cva_tia_has_concern": {"type": "bool", "description": ""},
    "cerebrovascular.subdural_hematoma": {
        "type": "bool",
        "description": "subdural hematoma",
    },
    "cerebrovascular.subdural_hematoma_has_concern": {
        "type": "bool",
        "description": "",
    },
    "cerebrovascular.subdural_hematoma_residual_neuro_deficit": {
        "type": "bool",
        "description": "residual neurological deficit after a subdural hematoma",
    },
    # --- Chronic Renal ---
    "chronicrenal.dialysis": {"type": "bool", "description": "dialysis"},
    "chronicrenal.dialysis_has_concern": {"type": "bool", "description": ""},
    "chronicrenal.kidney_stones": {"type": "bool", "description": "kidney stone"},
    "chronicrenal.nephrectomy": {"type": "bool", "description": "nephrectomy"},
    "chronicrenal.renal_failure": {"type": "bool", "description": "renal failure"},
    "chronicrenal.chronic_renal_failure": {
        "type": "bool",
        "description": "chronic renal failure",
    },
    "chronicrenal.renal_failure_has_concerns": {"type": "bool", "description": ""},
    "chronicrenal.renal_transplants": {
        "type": "bool",
        "description": "renal transplant, kidney transplant",
    },
    "chronicrenal.renal_transplants_has_concern": {"type": "bool", "description": ""},
    # --- Cardiovascular ---
    "cardiovascular.aortic_stenosis": {"type": "bool", "description": ""},
    "cardiovascular.aortic_stenosis_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.aortic_regurgitation": {"type": "bool", "description": ""},
    "cardiovascular.aortic_regurgitation_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.mitral_stenosis": {"type": "bool", "description": ""},
    "cardiovascular.mitral_stenosis_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.mitral_regurgitation": {"type": "bool", "description": ""},
    "cardiovascular.mitral_regurgitation_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.mitral_valve_prolapse": {"type": "bool", "description": "mitral valve prolapse, MVP"},
    "cardiovascular.mitral_valve_prolapse_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.cardiomyopathy": {"type": "bool", "description": ""},
    "cardiovascular.cardiomyopathy_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.ventricular_tachycardia": {
        "type": "bool",
        "description": "ventricular tachycardia, V-tach, VT (ventricular only -- not atrial or sinus tachycardia)",
    },
    "cardiovascular.ventricular_fibrillation": {
        "type": "bool",
        "description": "ventricular fibrillation, V-fib, VFib (ventricular only -- not atrial fibrillation)",
    },
    "cardiovascular.surgical_valve_repair": {
        "type": "bool",
        "description": "valve repair, valve replacement",
    },
    "cardiovascular.surgical_valve_repair_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.nyha_class": {"type": "int", "description": ""},
    "cardiovascular.lvef": {"type": "int", "description": ""},
    "cardiovascular.arrhythmia": {
        "type": "bool",
        "description": "arrhythmia, atrial fib, afib, a-fib, Tachycardia (rapid heart rate) Bradycardia (slow Heart Rate) Fibrillation/Flutter (abnormal heartbeat) Heart Block (I, II, BBB)",
    },
    "cardiovascular.arrhythmia_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.icd": {
        "type": "bool",
        "description": "ICD, implantable cardioverter",
    },
    "cardiovascular.icd_primary": {"type": "bool", "description": ""},
    "cardiovascular.icd_secondary": {"type": "bool", "description": ""},
    "cardiovascular.icd_date": {"type": "str", "description": ""},
    "cardiovascular.icd_therapy": {"type": "bool", "description": ""},
    "cardiovascular.icd_therapy_date": {"type": "str", "description": ""},
    "cardiovascular.loc": {"type": "bool", "description": "loss of consciousness, LOC"},
    "cardiovascular.cad": {
        "type": "bool",
        "description": "CAD, CABG, coronary artery, angina, angioplasty, stent, post-PCI, post-MI, heart attack",
    },
    "cardiovascular.cad_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.cad_date": {"type": "str", "description": ""},
    "cardiovascular.congestive_heart_failure": {"type": "bool", "description": "CHF"},
    "cardiovascular.congestive_heart_failure_cause": {"type": "str", "description": ""},
    "cardiovascular.congestive_heart_failure_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.cardiac_transplant": {
        "type": "bool",
        "description": "heart transplant",
    },
    "cardiovascular.cardiac_transplant_date": {"type": "str", "description": ""},
    "cardiovascular.cardiac_transplant_has_concerns": {
        "type": "bool",
        "description": "",
    },
    "cardiovascular.cholesterol": {"type": "bool", "description": ""},
    "cardiovascular.hypercholesterolemia": {"type": "bool", "description": ""},
    "cardiovascular.hyperlipidemia": {"type": "bool", "description": ""},
    "cardiovascular.dyslipidemia": {"type": "bool", "description": ""},
    "cardiovascular.hypertension": {"type": "bool", "description": "HTN"},
    "cardiovascular.pacemaker": {"type": "bool", "description": "permanent pacemaker, pacemaker, PPM"},
    "cardiovascular.pacemaker_date": {"type": "str", "description": ""},
    "cardiovascular.pacemaker_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.other": {"type": "str", "description": ""},
    "cardiovascular.syncope": {"type": "bool", "description": "syncope, fainting, fainted, blackout"},
    "cardiovascular.syncope_has_concerns": {"type": "bool", "description": ""},
    "cardiovascular.syncope_date": {"type": "str", "description": ""},
    "cardiovascular.syncope_cause": {"type": "str", "description": ""},
    # --- Endocrine ---
    "endocrine.diabetes": {"type": "bool", "description": ""},
    "endocrine.diabetes_has_concerns": {"type": "bool", "description": ""},
    "endocrine.diabetes.diet": {"type": "bool", "description": ""},
    "endocrine.diabetes.oral_meds": {"type": "bool", "description": ""},
    "endocrine.diabetes.insulin": {"type": "bool", "description": ""},
    "endocrine.diabetes.insulin_secretagogues": {"type": "bool", "description": ""},
    "endocrine.diabetes.non_compliant": {"type": "bool", "description": ""},
    "endocrine.stable_bg_control": {"type": "bool", "description": ""},
    "endocrine.HbA1C": {"type": "float", "description": ""},
    "endocrine.HbA1C_date": {"type": "str", "description": ""},
    "endocrine.HbA1C_has_concerns": {"type": "bool", "description": ""},
    "endocrine.hypothyroid": {"type": "bool", "description": ""},
    "endocrine.diabetes_insipidus": {
        "type": "bool",
        "description": "diabetes insipidus",
    },
    "endocrine.glycosuria_igt_impaired": {
        "type": "bool",
        "description": "glycosuria, glucose in urine",
    },
    "endocrine.glucose_tolerance": {
        "type": "bool",
        "description": "impaired glucose tolerance, IGT, prediabetes, pre-diabetes",
    },
    "endocrine.hypoglycemic_unawareness": {"type": "bool", "description": ""},
    "endocrine.hypoglycemic_unawareness_date": {"type": "str", "description": ""},
    "endocrine.severe_hypoglycemia": {"type": "bool", "description": ""},
    "endocrine.severe_hypoglycemia_date": {"type": "str", "description": ""},
    "endocrine.persistent_hypoglycemic_unawareness": {
        "type": "bool",
        "description": "",
    },
    "endocrine.persistent_hypoglycemic_unawareness_date": {
        "type": "str",
        "description": "",
    },
    "endocrine.diabetes_treatment_mentioned_in_text": {
        "type": "bool",
        "description": (
            "True when Section D / details_of_condition explicitly mentions a "
            "diabetes treatment or medication (insulin, oral medication/"
            "hypoglycemics, diet and exercise control, insulin secretagogues) "
            "even if none of the treatment checkboxes above are marked. Do not "
            "set this from a diabetes diagnosis alone -- only from explicit "
            "treatment/medication language in the free text."
        ),
    },
    "endocrine.other": {"type": "str", "description": ""},
    # --- General ---
    "general.general_debility": {
        "type": "bool",
        "description": "frailty, reduced reaction time, weakness",
    },
    "general.cancer": {
        "type": "bool",
        "description": "cancer, malignant, malignancy, carcinoma",
    },
    "general.cancer_has_concerns": {"type": "bool", "description": ""},
    "general.blood_disorders": {
        "type": "bool",
        "description": "blood disorder, anemia, anaemia, hemophilia, sickle cell, thrombocytopenia, bleeding disorder",
    },
    "general.christian_science_exemption": {
        "type": "bool",
        "description": "Christian Science exemption, religious exemption from medical examination or treatment",
    },
    "general.colostomy": {"type": "bool", "description": ""},
    "general.uro": {"type": "bool", "description": "urostomy, uro-ostomy"},
    "general.ileostomy": {"type": "bool", "description": ""},
    "general.crohns": {"type": "bool", "description": "Crohn's disease, Crohns"},
    "general.tremors": {"type": "bool", "description": "familial tremor, essential tremor, tremors"},
    "general.gout": {"type": "bool", "description": ""},
    "general.hemochromatosis": {"type": "bool", "description": ""},
    "general.hepatitis": {"type": "bool", "description": ""},
    "general.hernia": {"type": "bool", "description": "hernia"},
    "general.migraines": {"type": "bool", "description": "migraine, migraines"},
    "general.morbid_obesity": {"type": "bool", "description": " BMI"},
    "general.skin_conditions": {"type": "bool", "description": "eczema, psoriasis, skin condition"},
    "general.tupr": {"type": "bool", "description": "Transurethral Prostate Resection"},
    "general.ulcers": {"type": "bool", "description": "ulcer, ulcers, stomach ulcer, peptic ulcer"},
    "general.gerd": {"type": "bool", "description": "gastroesophageal reflux"},
    "general.stomach_ailments": {"type": "bool", "description": ""},
    "general.aids": {"type": "bool", "description": "AIDS, HIV"},
    "general.aids_has_concerns": {"type": "bool", "description": ""},
    # --- Hearing ---
    "hearing.cochlear_implants": {"type": "bool", "description": ""},
    "hearing.hearing_loss": {"type": "bool", "description": ""},
    "hearing.hearing_db_left": {"type": "int", "description": ""},
    "hearing.hearing_db_right": {"type": "int", "description": ""},
    "hearing.hearing_loss_one_ear": {"type": "bool", "description": ""},
    "hearing.hearing_aid_one_ear": {"type": "bool", "description": ""},
    "hearing.hearing_loss_has_concerns": {"type": "bool", "description": ""},
    "hearing.hearing_loss_high_frequency": {
        "type": "bool",
        "description": "high frequency hearing loss, high-frequency loss, loss of high tones/high-pitched sounds",
    },
    "hearing.passed_whisper_test": {"type": "bool", "description": ""},
    "hearing.mild_hearing_loss": {"type": "bool", "description": ""},
    "hearing.tinnitus": {"type": "bool", "description": ""},
    "hearing.non_progressive": {"type": "bool", "description": ""},
    "hearing.hearing_report_within_3_years": {"type": "bool", "description": ""},
    "hearing.profound_hearing_loss": {"type": "bool", "description": ""},
    "hearing.deaf": {"type": "bool", "description": ""},
    "hearing.other": {"type": "str", "description": ""},
    # --- CNS (continued) ---
    "cns.intracranial_tumors": {"type": "bool", "description": "intracranial tumor"},
    "cns.intracranial_tumors.resected": {"type": "bool", "description": ""},
    "cns.intracranial_tumors_details": {"type": "str", "description": ""},
    "cns.intracranial_tumors_has_concerns": {"type": "bool", "description": ""},
    "cns.multiple_sclerosis": {"type": "bool", "description": "MS, multiple sclerosis"},
    "cns.multiple_sclerosis_has_concerns": {"type": "bool", "description": ""},
    "cns.parkinsons": {"type": "bool", "description": "parkinson"},
    "cns.parkinsons_has_concerns": {"type": "bool", "description": ""},
    "cns.mitochondrial_myopathy": {
        "type": "bool",
        "description": "mitochondrial myopathy",
    },
    "cns.mitochondrial_myopathy_has_concerns": {"type": "bool", "description": ""},
    "cns.muscular_dystrophy": {"type": "bool", "description": "muscular dystrophy"},
    "cns.muscular_dystrophy_has_concerns": {"type": "bool", "description": ""},
    "cns.myasthenia_gravis": {"type": "bool", "description": "myasthenia gravis"},
    "cns.myasthenia_gravis_has_concerns": {"type": "bool", "description": ""},
    "cns.charcot_marie_tooth_disease": {
        "type": "bool",
        "description": "charcot-marie-tooth, CMT",
    },
    "cns.charcot_marie_tooth_disease_has_concerns": {"type": "bool", "description": ""},
    "cns.als": {"type": "bool", "description": "ALS, amyotrophic lateral sclerosis"},
    "cns.als_has_concerns": {"type": "bool", "description": ""},
    "cns.progressive_deficit": {"type": "bool", "description": ""},
    "cns.progressive_deficit_has_concerns": {"type": "bool", "description": ""},
    "cns.non_progressive_stable": {
        "type": "bool",
        "description": "cerebral palsy, unspecified plegia",
    },
    "cns.non_progressive_stable_has_concerns": {"type": "bool", "description": ""},
    "cns.peripheral_neuropathy": {"type": "bool", "description": ""},
    "cns.peripheral_neuropathy_has_concerns": {"type": "bool", "description": ""},
    "cns.epilepsy": {"type": "bool", "description": "seizure"},
    "cns.provoked_seizure": {"type": "bool", "description": ""},
    "cns.seizure_date": {"type": "str", "description": ""},
    "cns.seizure_cause": {"type": "str", "description": ""},
    # --- Musculoskeletal ---
    "musculoskeletal.limb_amputation": {"type": "bool", "description": ""},
    "musculoskeletal.limb_amputation_date": {"type": "str", "description": ""},
    "musculoskeletal.limb_amputation_cause": {"type": "str", "description": ""},
    "musculoskeletal.amputation_right_sided": {"type": "bool", "description": ""},
    "musculoskeletal.amputation_left_sided": {"type": "bool", "description": ""},
    "musculoskeletal.amputation_above_knee": {"type": "bool", "description": ""},
    "musculoskeletal.amputation_below_knee": {"type": "bool", "description": ""},
    "musculoskeletal.amputation_lower_limb": {"type": "bool", "description": ""},
    "musculoskeletal.amputation_upper_limb": {"type": "bool", "description": ""},
    "musculoskeletal.vehicle_modifications": {"type": "bool", "description": ""},
    "musculoskeletal.vehicle_modifications_details": {"type": "str", "description": ""},
    "musculoskeletal.limb_amputation_fingers_or_toes": {
        "type": "bool",
        "description": "",
    },
    "musculoskeletal.arthritis": {"type": "bool", "description": ""},
    "musculoskeletal.ankylosing_spondylitis": {"type": "bool", "description": ""},
    "musculoskeletal.rheumatoid_arthritis": {"type": "bool", "description": "RA"},
    "musculoskeletal.osteoarthritis": {"type": "bool", "description": "OA"},
    "musculoskeletal.osteoporosis": {"type": "bool", "description": ""},
    "musculoskeletal.lupus": {"type": "bool", "description": "SLE"},
    "musculoskeletal.back_pain": {"type": "bool", "description": ""},
    "musculoskeletal.degenerative_disk_disease": {"type": "bool", "description": "DDD"},
    "musculoskeletal.fibromyalgia": {"type": "bool", "description": ""},
    "musculoskeletal.polymyalgia": {"type": "bool", "description": ""},
    "musculoskeletal.foot_drop": {"type": "bool", "description": ""},
    "musculoskeletal.foot_drop_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.paraplegia": {"type": "bool", "description": "paraplegia"},
    "musculoskeletal.paraplegia_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.quadriplegia": {"type": "bool", "description": ""},
    "musculoskeletal.quadriplegia_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.tetraplegia": {"type": "bool", "description": ""},
    "musculoskeletal.tetraplegia_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.spinal_bifida": {"type": "bool", "description": "spina bifida, spinal bifida"},
    "musculoskeletal.spinal_bifida_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.polio": {"type": "bool", "description": ""},
    "musculoskeletal.post_polio": {"type": "bool", "description": "post-polio, post-polio syndrome"},
    "musculoskeletal.range_of_motion_loss": {"type": "bool", "description": "range of motion loss, reduced range of motion, limited range of motion"},
    "musculoskeletal.range_of_motion_loss_details": {"type": "str", "description": ""},
    "musculoskeletal.spinal_injury": {"type": "bool", "description": ""},
    "musculoskeletal.neck_injury": {"type": "bool", "description": ""},
    "musculoskeletal.back_injury": {"type": "bool", "description": ""},
    "musculoskeletal.restless_leg_syndrome": {"type": "bool", "description": "restless leg syndrome, restless legs, RLS"},
    "musculoskeletal.spinal_stenosis": {"type": "bool", "description": ""},
    "musculoskeletal.dwarfism": {"type": "bool", "description": ""},
    "musculoskeletal.dwarfism_has_concerns": {"type": "bool", "description": ""},
    "musculoskeletal.weakness": {"type": "bool", "description": ""},
    "musculoskeletal.weakness_details": {"type": "str", "description": ""},
    "musculoskeletal.other": {"type": "str", "description": ""},
    # --- PVD ---
    "pvd.abdominal_aortic_aneurysm": {
        "type": "bool",
        "description": "abdominal aortic aneurysm, AAA",
    },
    "pvd.abdominal_aortic_aneurysm_repaired": {
        "type": "bool",
        "description": "clipped, treated",
    },
    "pvd.abdominal_aortic_aneurysm_has_concerns": {
        "type": "bool",
        "description": "imminent rupture risk",
    },
    "pvd.aneurysm": {"type": "bool", "description": ""},
    "pvd.aneurysm_site": {"type": "str", "description": ""},
    "pvd.aneurysm_size": {
        "type": "float",
        "description": "check details_of_condition in case size is written there",
    },
    "pvd.aortic_dissection": {"type": "bool", "description": "aortic dissection"},
    "pvd.aortic_dissection_has_concerns": {"type": "bool", "description": ""},
    "pvd.carotid_stenosis": {"type": "bool", "description": "carotid stenosis"},
    "pvd.carotid_stenosis_loss_consciousness": {
        "type": "bool",
        "description": "loss of consciousness, syncope, LOC associated with carotid stenosis",
    },
    "pvd.claudication": {"type": "bool", "description": ""},
    "pvd.deep_vein_thrombosis": {"type": "bool", "description": "DVT"},
    "pvd.peripheral_arterial_disease": {"type": "bool", "description": "PAD"},
    "pvd.peripheral_vascular_disease": {"type": "bool", "description": "PVD"},
    "pvd.peripheral_vascular_disease_site": {"type": "str", "description": ""},
    # --- Psychiatric ---
    "psychiatric.adhd": {"type": "bool", "description": ""},
    "psychiatric.add": {"type": "bool", "description": ""},
    "psychiatric.ocd": {"type": "bool", "description": "obsessive-compulsive"},
    "psychiatric.anxiety": {"type": "bool", "description": ""},
    "psychiatric.ptsd": {"type": "bool", "description": "post-traumatic stress"},
    "psychiatric.mild_depression": {
        "type": "bool",
        "description": "depression, depressive",
    },
    "psychiatric.autism": {"type": "bool", "description": "ASD"},
    "psychiatric.autism_has_concerns": {"type": "bool", "description": ""},
    "psychiatric.mental_handicap": {
        "type": "bool",
        "description": "low IQ, IQ concerns, intellectual disability, mental handicap",
    },
    "psychiatric.mental_handicap_has_concerns": {"type": "bool", "description": ""},
    "psychiatric.psychosis": {"type": "bool", "description": "psychotic"},
    "psychiatric.psychosis_date": {"type": "str", "description": ""},
    "psychiatric.psychosis_has_concerns": {"type": "bool", "description": ""},
    "psychiatric.impaired_judgement": {"type": "bool", "description": ""},
    "psychiatric.stable_condition": {"type": "bool", "description": ""},
    "psychiatric.unstable_condition": {"type": "bool", "description": ""},
    "psychiatric.compliant_with_treatment": {"type": "bool", "description": ""},
    "psychiatric.non_compliant_with_treatment": {"type": "bool", "description": ""},
    "psychiatric.on_medications": {
        "type": "bool",
        "description": "on medications for a psychiatric condition",
    },
    "psychiatric.bipolar": {"type": "bool", "description": ""},
    "psychiatric.bipolar_has_concerns": {"type": "bool", "description": ""},
    "psychiatric.schizophrenia": {"type": "bool", "description": ""},
    "psychiatric.schizophrenia_has_concerns": {"type": "bool", "description": ""},
    "psychiatric.other": {"type": "str", "description": ""},
    # True when the "other psych diagnosis" checkbox is ticked, or the
    # psychiatric.other / psychiatric.psych_diagnosis text is filled in (set
    # deterministically, see pipeline.derive_other_psych_diagnosis), or
    # Section D names another psychiatric diagnosis.
    "psychiatric.other_psych_diagnosis": {
        "type": "bool",
        "description": "a psychiatric diagnosis other than those listed, e.g. severe depression",
    },
    "psychiatric.other_psych_diagnosis_has_concerns": {
        "type": "bool",
        "description": "",
    },
    # --- Psychotropic Drugs ---
    "psychotropic_drugs.perscribed_drugs": {"type": "bool", "description": ""},
    "psychotropic_drugs.perscribed_drugs_compliant": {
        "type": "bool",
        "description": "",
    },
    "psychotropic_drugs.perscribed_drugs_non_compliant": {
        "type": "bool",
        "description": "",
    },
    "psychotropic_drugs.perscribed_drugs_has_concerns": {
        "type": "bool",
        "description": "",
    },
    # The form's two checkboxes read "Substance Use Disorder (SUD) diagnosed,
    # ACTIVE" and "Alcohol Use Disorder (AUD) diagnosed, ACTIVE" -- true means
    # an active diagnosis. Past abuse/dependence goes in
    # substance_abuse_or_dependence instead.
    "psychotropic_drugs.substance_use": {
        "type": "bool",
        "description": "Substance Use Disorder (SUD) diagnosed, ACTIVE -- the form checkbox, or text stating a current/active SUD diagnosis",
    },
    "psychotropic_drugs.alcohol_use": {
        "type": "bool",
        "description": "Alcohol Use Disorder (AUD) diagnosed, ACTIVE -- the form checkbox, or text stating a current/active AUD diagnosis",
    },
    "psychotropic_drugs.substance_abuse_or_dependence": {
        "type": "bool",
        "description": "substance or alcohol abuse or dependence within the last 2 years, or a substance/alcohol use disorder mentioned without it being stated as active",
    },
    "psychotropic_drugs.substance_use_has_concerns": {
        "type": "bool",
        "description": "concerns specific to the substance or alcohol use",
    },
    "psychotropic_drugs.alcohol_withdrawal_seizure_date": {
        "type": "str",
        "description": "",
    },
    "psychotropic_drugs.psychoactive_drugs_details": {"type": "str", "description": ""},
    "psychotropic_drugs.narcotics_details": {"type": "str", "description": ""},
    "psychotropic_drugs.other": {"type": "str", "description": ""},
    # --- Sleep ---
    "sleep.obstructive_sleep_apnea": {
        "type": "bool",
        "description": "OSA, obstructive sleep apnea, sleep apnea",
    },
    "sleep.osa_mild": {"type": "bool", "description": ""},
    "sleep.osa_mod": {"type": "bool", "description": ""},
    "sleep.osa_severe": {"type": "bool", "description": ""},
    "sleep.obstructive_sleep_apnea_has_concerns": {"type": "bool", "description": ""},
    "sleep.ahi_score": {"type": "int", "description": ""},
    "sleep.epworth_score": {"type": "int", "description": ""},
    "sleep.cpap": {"type": "bool", "description": ""},
    "sleep.cpap_compliant": {"type": "bool", "description": ""},
    "sleep.cpap_non_compliant": {"type": "bool", "description": ""},
    "sleep.no_daytime_sleepiness": {"type": "bool", "description": ""},
    "sleep.with_daytime_sleepiness": {"type": "bool", "description": ""},
    "sleep.insomnia": {"type": "bool", "description": ""},
    "sleep.narcolepsy": {"type": "bool", "description": "narcolepsy, cataplexy, sleep attacks"},
    "sleep.narcolepsy_has_concerns": {"type": "bool", "description": ""},
    "sleep.narcolepsy_over_12_months": {"type": "bool", "description": ""},
    "sleep.narcolepsy_under_12_months": {"type": "bool", "description": ""},
    "sleep.narcolepsy_controlled": {
        "type": "bool",
        "description": "check details_of_condition for mention of controlled",
    },
    "sleep.narcolepsy_uncontrolled": {
        "type": "bool",
        "description": "check details_of_condition for mention of uncontrolled",
    },
    "sleep.other": {"type": "str", "description": ""},
    # --- Traumatic Brain Injury ---
    "traumatic_brain_injury": {
        "type": "bool",
        "description": "TBI, head injury, concussion",
    },
    "traumatic_brain_injury.has_concerns": {"type": "bool", "description": ""},
    "traumatic_brain_injury.vehicle_modifications": {"type": "bool", "description": ""},
    "traumatic_brain_injury.complex_deficits": {"type": "bool", "description": ""},
    # --- Vestibular ---
    "vestibular.drop_attacks": {"type": "bool", "description": "drop attacks, Tumarkin's crisis, Tumarkin otolithic crisis"},
    "vestibular.drop_attacks_has_concerns": {"type": "bool", "description": ""},
    "vestibular.drop_attack_date": {"type": "str", "description": ""},
    "vestibular.recurrent_vertigo": {
        "type": "bool",
        "description": "hyperventilation syndrome, vertigo, Meniere's disease, Vestibular neuronitis, psychogenic vertigo",
    },
    "vestibular.recurrent_vertigo_has_concerns": {
        "type": "bool",
        "description": "look specifically for any concerns, not just the condition",
    },
    "vestibular.vertigo_with_warnings": {"type": "bool", "description": ""},
    "vestibular.vertigo_without_warnings": {"type": "bool", "description": ""},
    "vestibular.vertigo_date": {"type": "str", "description": ""},
    # --- Visual Acuity (thresholds) — see prompts.py for evaluation rules ---
    "visual_acuity.corrected_vision_20/80_or_worse": {
        "type": "bool",
        "description": "Deterministic -- computed post-LLM by pipeline.apply_visual_acuity_thresholds(), which overwrites any value the model sets here. Corrected only: corrected_both, else the better of corrected_left/right.",
    },
    "visual_acuity.corrected_vision_20/60_or_worse": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected only: corrected_both, else the better of corrected_left/right.",
    },
    "visual_acuity.corrected_vision_20/50_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected only: corrected_both, else the better of corrected_left/right. False when no corrected value exists.",
    },
    "visual_acuity.corrected_vision_20/30_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected only: corrected_both, else the better of corrected_left/right. False when no corrected value exists.",
    },
    "visual_acuity.corrected_vision_20/20_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected only: corrected_both, else the better of corrected_left/right. False when no corrected value exists.",
    },
    "visual_acuity.vision_20/60_or_worse": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first (both, else better of left/right), falling back to uncorrected only when no corrected value exists at all.",
    },
    "visual_acuity.vision_20/40_or_worse": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first (both, else better of left/right), falling back to uncorrected only when no corrected value exists at all.",
    },
    "visual_acuity.vision_20/50_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first (both, else better of left/right), falling back to uncorrected only when no corrected value exists at all.",
    },
    "visual_acuity.vision_20/30_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first (both, else better of left/right), falling back to uncorrected only when no corrected value exists at all.",
    },
    "visual_acuity.vision_20/20_or_better": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first (both, else better of left/right), falling back to uncorrected only when no corrected value exists at all.",
    },
    "visual_acuity.bad_eye_20/100_or_worse": {
        "type": "bool",
        "description": "Deterministic -- see pipeline.apply_visual_acuity_thresholds(). Corrected first, using the WORSE of left/right (never the both-eyes value -- this field asks about the bad eye specifically), falling back to uncorrected only when no corrected value exists at all.",
    },
    # --- Priority ---
    "priority.should_not_drive": {
        "type": "bool",
        "description": "look for any words like should not drive, can not drive, can't drive now",
    },
    "priority.should_not_drive_has_reason": {
        "type": "bool",
        "description": "look for reason for should not drive",
    },
    "priority.applying_for_class": {"type": "bool", "description": ""},
    "priority.applying_for_class_has_class": {
        "type": "bool",
        "description": "check to see if current_license_class contains the class driver is applying for",
    },
    "priority.unfit_for_current_class": {
        "type": "bool",
        "description": "fit for downgrade",
    },
    "priority.has_concerns": {
        "type": "bool",
        "description": "look for any concerns about patient's ability to drive",
    },
    # --- Respiratory ---
    "respiratory.asthma": {"type": "bool", "description": "asthma"},
    "respiratory.copd": {"type": "bool", "description": "COPD, chronic obstructive pulmonary disease"},
    "respiratory.emphysema": {"type": "bool", "description": "emphysema"},
    "respiratory.other_respiratory_condition": {"type": "bool", "description": ""},
    "respiratory.has_concerns": {"type": "bool", "description": ""},
    "respiratory.oxygen_use": {"type": "bool", "description": "oxygen use, O2"},
    "respiratory.permanent_tracheostomy": {
        "type": "bool",
        "description": "tracheostomy",
    },
    "respiratory.pulmonary_embolism": {
        "type": "bool",
        "description": "pulmonary embolism, PE",
    },
    "respiratory.pulmonary_embolism_resolved": {"type": "bool", "description": ""},
    "respiratory.pulmonary_embolism_has_concerns": {"type": "bool", "description": ""},
}

# ---------------------------------------------------------------------------
# Deterministic meta fields -- computed after the LLM analysis pass, not
# sent to it for evaluation (same precedent as `guide`, `current_licence_class`,
# `restrictions`, etc.: administrative/derived fields kept out of CONDITIONS
# entirely, rather than given a category mapping they don't have).
#
#   no_other_conditions               -- see pipeline.check_no_other_conditions()
#   guide_with_no_matching_condition  -- see pipeline.check_guide_matching()
#   diabetes_guide_9_1                -- see pipeline.check_diabetes_guide_9_1()
#   endocrine.diabetes_treatment_not_indicated -- see pipeline.check_diabetes_treatment_not_indicated()
#   non_vision_condition_indicated    -- see pipeline.check_no_other_conditions()
# ---------------------------------------------------------------------------
META_FIELD_DEFAULTS: dict[str, bool] = {
    "no_other_conditions": False,
    "non_vision_condition_indicated": False,
    "guide_with_no_matching_condition": False,
    "diabetes_guide_9_1": False,
    "endocrine.diabetes_treatment_not_indicated": False,
}


class ConditionCategory(str, Enum):
    """Structured categories used by the first LLM call."""

    TOP_LEVEL = "top_level"
    VISION = "vision"
    COGNITION = "cognition"
    VISUAL_ACUITY = "visual_acuity"
    VISUAL_FIELD = "visual_field"
    RECOMMENDATIONS = "recommendations"
    CEREBROVASCULAR = "cerebrovascular"
    CHRONIC_RENAL = "chronic_renal"
    CARDIOVASCULAR = "cardiovascular"
    ENDOCRINE = "endocrine"
    GENERAL = "general"
    HEARING = "hearing"
    CNS = "cns"
    MUSCULOSKELETAL = "musculoskeletal"
    PVD = "pvd"
    PSYCHIATRIC = "psychiatric"
    PSYCHOTROPIC_DRUGS = "psychotropic_drugs"
    SLEEP = "sleep"
    TRAUMATIC_BRAIN_INJURY = "traumatic_brain_injury"
    VESTIBULAR = "vestibular"
    PRIORITY = "priority"
    RESPIRATORY = "respiratory"


CATEGORY_PREFIXES: dict[ConditionCategory, tuple[str, ...]] = {
    ConditionCategory.TOP_LEVEL: (
        "current_licence_class",
        "blood_pressure",
        "restrictions",
        "medical_examination_date",
        "details_of_condition",
    ),
    ConditionCategory.VISION: ("vision.",),
    ConditionCategory.COGNITION: (
        "cns.cognitiveimpairment",
        "cns.dementia",
        "cns.alzheimers",
        "cns.mmse_score",
        "cns.moca_score",
        "cns.simard_score",
        "cns.gds_level_score",
        "cns.trails_",
        "cns.significant_head_injury_date",
    ),
    ConditionCategory.VISUAL_ACUITY: ("visual_acuity.",),
    ConditionCategory.VISUAL_FIELD: ("visual_field.",),
    ConditionCategory.RECOMMENDATIONS: ("recommendations.",),
    ConditionCategory.CEREBROVASCULAR: ("cerebrovascular.",),
    ConditionCategory.CHRONIC_RENAL: ("chronicrenal.",),
    ConditionCategory.CARDIOVASCULAR: ("cardiovascular.",),
    ConditionCategory.ENDOCRINE: ("endocrine.",),
    ConditionCategory.GENERAL: ("general.",),
    ConditionCategory.HEARING: ("hearing.",),
    ConditionCategory.CNS: ("cns.",),
    ConditionCategory.MUSCULOSKELETAL: ("musculoskeletal.",),
    ConditionCategory.PVD: ("pvd.",),
    ConditionCategory.PSYCHIATRIC: ("psychiatric.",),
    ConditionCategory.PSYCHOTROPIC_DRUGS: ("psychotropic_drugs.",),
    ConditionCategory.SLEEP: ("sleep.",),
    ConditionCategory.TRAUMATIC_BRAIN_INJURY: ("traumatic_brain_injury",),
    ConditionCategory.VESTIBULAR: ("vestibular.",),
    ConditionCategory.PRIORITY: ("priority.",),
    ConditionCategory.RESPIRATORY: ("respiratory.",),
}


# ---------------------------------------------------------------------------
# BC Guide section number -> the category/categories that section covers.
#
# This is the table of contents' *section* numbering (the coarse, stable
# grouping), not the finer-grained chapter content within each section --
# chapter content is what's "liable to change" and is deliberately not
# encoded here. Section numbers should renumber far less often than the
# specific criteria inside a chapter, since renumbering breaks every
# printed/scanned form already in circulation referencing the old number.
#
# Section 7 alone covers seven different chapters (Intracranial Tumors,
# Neurological Disorders, Seizures and Epilepsy, Sleep Disorders, Syncope,
# Traumatic Brain Injuries, Vestibular Disorders) -- it cannot be narrowed
# to one ConditionCategory, so it maps to all of them; a match against any
# one is treated as a match for the guide reference (see check_guide_matching
# in pipeline.py).
#
# Sections not listed here (e.g. there is no "Section 16" in the current
# table of contents) are deliberately absent, not a gap to fill in --
# check_guide_matching treats an unrecognized section number the same as
# "can't judge", defaulting to not flagging a mismatch (a false positive on
# guide_with_no_matching_condition is worse than a missed one here).
# ---------------------------------------------------------------------------
GUIDE_SECTION_CATEGORIES: dict[int, tuple[ConditionCategory, ...]] = {
    2: (ConditionCategory.VISION,),
    3: (ConditionCategory.HEARING,),
    4: (ConditionCategory.CARDIOVASCULAR,),
    5: (ConditionCategory.CEREBROVASCULAR,),
    6: (ConditionCategory.PVD,),
    7: (
        ConditionCategory.CNS,
        ConditionCategory.SLEEP,
        ConditionCategory.CARDIOVASCULAR,
        ConditionCategory.TRAUMATIC_BRAIN_INJURY,
        ConditionCategory.VESTIBULAR,
    ),
    8: (ConditionCategory.RESPIRATORY,),
    9: (ConditionCategory.ENDOCRINE,),
    10: (ConditionCategory.CHRONIC_RENAL,),
    11: (ConditionCategory.MUSCULOSKELETAL,),
    12: (ConditionCategory.PSYCHIATRIC,),
    13: (ConditionCategory.PSYCHOTROPIC_DRUGS,),
    14: (ConditionCategory.PSYCHOTROPIC_DRUGS,),
    15: (ConditionCategory.COGNITION,),
    17: (ConditionCategory.GENERAL,),
}


CATEGORY_CONDITIONS: dict[ConditionCategory, dict[str, dict]] = {
    category: {
        name: cfg
        for name, cfg in CONDITIONS.items()
        if any(name == prefix or name.startswith(prefix) for prefix in prefixes)
    }
    for category, prefixes in CATEGORY_PREFIXES.items()
}

for field_name in CATEGORY_CONDITIONS[ConditionCategory.COGNITION]:
    CATEGORY_CONDITIONS[ConditionCategory.CNS].pop(field_name, None)


CATEGORY_INSTRUCTIONS: dict[ConditionCategory, str] = {
    ConditionCategory.VISION: (
        "Treat procedures like cataract extraction as evidence for both the condition "
        "and the procedure/surgery field. For example, 'BIL CATARACT EXTRACTIONS' "
        "means vision.cataracts=true and vision.cataracts_had_surgery=true. BIL/B/L "
        "means bilateral. Do not mark has_concerns true when the text only names the "
        "condition. Macular degeneration, glaucoma, cataracts and retinopathy all "
        "mean vision.progressive_eye_condition=true."
    ),
    ConditionCategory.VISUAL_ACUITY: (
        "Normalize Snellen acuity. Vision acuity text may be misread with the slash "
        "being read as 1, e.g. 20/80 might be read as 20180. If this is the case, "
        "correct it. Convert acuity to Snellen before comparing; higher denominator "
        "means worse. Decimal: denom = 20/value, e.g. 0.25 means 20/80. LogMAR: "
        "denom = 20 x 10^value, e.g. 0.6 means 20/80. Follow each field's description "
        "for which eyes/values to use. Evaluate every threshold independently. "
        "Example: 20/80 means _or_worse for 20/80, 20/60, and 20/40 are true; "
        "_or_better for 20/50 and 20/30 are false. If only one eye has data, use it "
        "where L/R is needed. Return all visual_acuity threshold fields when any "
        "acuity value is present."
    ),
    ConditionCategory.VISUAL_FIELD: (
        "Use visual field checkbox evidence directly. If the form says the visual "
        "field is abnormal, evaluate abnormal_has_concerns separately from abnormal."
    ),
    ConditionCategory.RECOMMENDATIONS: (
        "Do not make clinical or administrative judgements in this category. Only set "
        "recommendations.road_test_to_assess to true when written text specifically "
        "recommends, requests, orders, or clearly suggests a road test. Do not infer a "
        "road test from medical conditions, vision thresholds, concerns, licence class, "
        "or general fitness risk alone. Copy written recommendation/rationale/restriction "
        "text into the matching string fields when present."
    ),
    ConditionCategory.PRIORITY: (
        "Only fill priority fields when the text explicitly contains priority language "
        "about driving status, driving safety, ability to drive, being unfit for the "
        "current class, being told not to drive, or applying for a licence class. Do "
        "not infer priority fields solely from diagnoses, symptoms, measurements, or "
        "other matched medical conditions."
    ),
    ConditionCategory.COGNITION: (
        "Focus on cognitive impairment, dementia, Alzheimer's disease, cognitive "
        "test scores, functional concerns, and suspected section 7.15 impairment."
    ),
    ConditionCategory.CARDIOVASCULAR: (
        "Recognize common abbreviations such as CAD, CABG, PCI, MI, CHF, Afib, ICD, "
        "MVP, HTN, LOC, and s/p cardiac procedures. For example, 's/p CABG' means "
        "cardiovascular.cad=true. A valve repair or replacement procedure implies "
        "cardiovascular.surgical_valve_repair=true. "
        "Loss of consciousness or syncope caused by carotid stenosis maps to "
        "pvd.carotid_stenosis_loss_consciousness — set cardiovascular.loc or "
        "cardiovascular.syncope only when the LOC has a cardiac origin (arrhythmia, "
        "valve disease, etc.)."
    ),
    ConditionCategory.ENDOCRINE: (
        "Recognize diabetes variants including DM, IDDM, NIDDM, insulin-dependent, "
        "secretagogues, HbA1C, hypoglycemia, and compliance language. For example, "
        "'insulin-dependent DM' means endocrine.diabetes=true and "
        "endocrine.diabetes.insulin=true. HbA1C values map to endocrine.HbA1C and "
        "do not alone make endocrine.HbA1C_has_concerns true without qualitative "
        "concern text."
    ),
    ConditionCategory.PVD: (
        "For aneurysm fields, map size/site to their fields; for example, 'size "
        "6.9 cm' maps to pvd.aneurysm_size. A size alone is not a concern unless "
        "qualitative risk language such as rupture is present. A repair, clip, "
        "treatment, s/p procedure, or similar procedure implies the matching "
        "repaired/procedure field when one exists. "
        "Aortic dissection maps to pvd.aortic_dissection; "
        "carotid stenosis maps to pvd.carotid_stenosis."
    ),
    ConditionCategory.PSYCHIATRIC: (
        "Separate diagnoses from stability, treatment compliance, impaired judgement, "
        "and psychosis concerns. Severe depression maps to other_psych_diagnosis. "
        "Low IQ, intellectual disability, and mental handicap map to "
        "psychiatric.mental_handicap."
    ),
    ConditionCategory.SLEEP: (
        "Recognize OSA/sleep apnea, CPAP use and compliance, AHI, Epworth, daytime "
        "sleepiness, insomnia, and narcolepsy control status. For example, 'OSA on "
        "CPAP' means sleep.obstructive_sleep_apnea=true and sleep.cpap=true."
    ),
    ConditionCategory.VESTIBULAR: (
        "Map vertigo, Meniere's disease, vestibular neuronitis, psychogenic vertigo, "
        "and hyperventilation syndrome to recurrent_vertigo."
    ),
    ConditionCategory.CNS: (
        "For neurological disease, distinguish stable/non-progressive deficits from "
        "progressive deficits and extract seizure dates/causes when present. Treat "
        "s/p resection as evidence for the matching tumor/procedure fields when present. "
        "Unspecified 'plegia' (without a qualifier such as para, quad, tetra, or hemi) "
        "maps to cns.non_progressive_stable."
    ),
    ConditionCategory.CEREBROVASCULAR: (
        "Recognize CVA, stroke, TIA, cerebral aneurysm, subdural hematoma, dates, "
        "repair status, and residual neurological deficits. For example, 'hx of TIA' "
        "means cerebrovascular.tia=true. A repaired or s/p cerebral aneurysm procedure "
        "implies the aneurysm condition and repaired field."
    ),
    ConditionCategory.CHRONIC_RENAL: (
        "Recognize dialysis, kidney stones, renal failure, chronic renal failure, "
        "nephrectomy, renal transplant, and kidney transplant. A transplant or "
        "nephrectomy procedure implies both the relevant renal condition/procedure "
        "field and any matching concern field only when concern text is present."
    ),
    ConditionCategory.HEARING: (
        "Recognize hearing loss, deafness, tinnitus, cochlear implants, hearing aids, "
        "whisper test results, high-frequency loss, and left/right decibel values. A "
        "cochlear implant procedure implies hearing.cochlear_implants=true."
    ),
    ConditionCategory.MUSCULOSKELETAL: (
        "Extract amputation side/level, vehicle modifications, weakness, range of "
        "motion loss, spinal injury, and arthritis variants. "
        "BIL/B/L means bilateral. s/p amputation or injury repair should be mapped to "
        "the matching condition/procedure fields when present. "
        "Set paraplegia, quadriplegia, or tetraplegia only when those exact words appear."
    ),
    ConditionCategory.RESPIRATORY: (
        "Recognize asthma, COPD, emphysema, oxygen/O2 use, tracheostomy, pulmonary "
        "embolism, resolution, and respiratory concern language."
    ),
}


ALWAYS_ANALYZE_CATEGORIES = (
    ConditionCategory.TOP_LEVEL,
    ConditionCategory.PRIORITY,
)

# ---------------------------------------------------------------------------
# Fields to extract from the DMER JSON before sending to the LLM.
# These are the free-text / value fields that carry useful context.
# Boolean checkbox fields are handled separately (True ones are auto-included).
# ---------------------------------------------------------------------------
EXTRACT_FIELDS = [
    "vision.acuity_loss_cause",
    "vision.field_defect_cause",
    "vision.monocular_date",
    "visual_field.meet_criteria_for_licence_class_yes",
    "vision.other",
    "vestibular.drop_attack_date",
    "vestibular.vertigo_date",
    "hearing.other",
    "musculoskeletal.limb_amputation_date",
    "musculoskeletal.limb_amputation_cause",
    "musculoskeletal.vehicle_modifications_details",
    "musculoskeletal.weakness_details",
    "musculoskeletal.range_of_motion_loss_details",
    "musculoskeletal.other",
    "cardiovascular.syncope_date",
    "cardiovascular.syncope_cause",
    "cardiovascular.cad_date",
    "cardiovascular.arrhythmia_type",
    "cardiovascular.pacemaker_date",
    "cardiovascular.icd_date",
    "cardiovascular.icd_therapy_date",
    "cardiovascular.congestive_heart_failure",
    "cardiovascular.congestive_heart_failure_cause",
    "cardiovascular.lvef",
    "cardiovascular.nyha_class",
    "pvd.aneurysm_site",
    "pvd.aneurysm_size",
    "pvd.peripheral_vascular_disease_site",
    "cardiovascular.other",
    "psychiatric.psychosis_date",
    "psychiatric.psych_diagnosis",
    "psychiatric.other",
    "cerebrovascular.cva_tia_date",
    "cns.seizure_cause",
    "cns.seizure_date",
    "cns.moca_score",
    "cns.trails_a_seconds",
    "cns.trails_b_seconds",
    "cns.dementia_details",
    "cns.significant_head_injury_date",
    "cns.intracranial_tumors_details",
    "cns.other",
    "psychotropic_drugs.alcohol_withdrawal_seizure_date",
    "psychotropic_drugs.psychoactive_drugs_details",
    "psychotropic_drugs.narcotics_details",
    "psychotropic_drugs.other",
    "sleep.ahi",
    "sleep.ahi_score",
    "sleep.epworth_score",
    "sleep.other",
    "endocrine.HbA1C",
    "endocrine.HbA1C_date",
    "endocrine.severe_hypoglycemia_date",
    "endocrine.hypoglycemic_unawareness_date",
    "endocrine.persistent_hypoglycemic_unawareness_date",
    "endocrine.other",
    "general.other",
    "visual_acuity.corrected_left",
    "visual_acuity.corrected_right",
    "visual_acuity.corrected_both",
    "visual_acuity.uncorrected_left",
    "visual_acuity.uncorrected_right",
    "visual_acuity.uncorrected_both",
    "opinion.maybe_followup_years",
    "details_of_condition",
    "current_license_class",
    "applying_for_class_number",
    "recommendations.specialist_consult_type",
    "recommendations.road_test_to_assess_details",
    "recommendations.rationale_for_road_test",
    "recommendations.restrictions_reason_and_type",
    "relationship_with_patient.family_physician_years",
    "relationship_with_patient.exam_date",
]

# Default values per type — used when backfilling missing fields
TYPE_DEFAULTS: dict[str, object] = {
    "bool": False,
    "str": "",
    "int": None,
    "float": None,
}
