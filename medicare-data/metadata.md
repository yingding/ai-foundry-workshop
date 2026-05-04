# Medicare Data – Schema Documentation

This folder contains two mock CSV datasets for a lung cancer patient cohort study.
Both files share the `PATIENT_ID` column as join key and are designed to be merged
via an inner join to produce a complete patient profile.

## lung_cancer_patient_clinical_features.csv

**Rows**: 3,000 patients (PATIENT_ID 0–2999)

| Column                  | Type    | Description |
|-------------------------|---------|-------------|
| PATIENT_ID              | int     | Unique patient identifier (0–2999) |
| GENDER                  | str     | M or F |
| AGE                     | int     | Patient age at time of data collection |
| SMOKING                 | str     | Yes / No – current or former smoker |
| YELLOW_FINGERS          | str     | Yes / No – yellowing of fingers (smoking indicator) |
| ANXIETY                 | str     | Yes / No – clinically significant anxiety |
| PEER_PRESSURE           | str     | Yes / No – social pressure contributing to risk behaviors |
| CHRONIC_DISEASE         | str     | Yes / No – presence of chronic comorbidities (COPD, diabetes, etc.) |
| FATIGUE                 | str     | Yes / No – persistent fatigue |
| ALLERGY                 | str     | Yes / No – known allergies |
| WHEEZING               | str     | Yes / No – audible wheezing |
| ALCOHOL_CONSUMING       | str     | Yes / No – regular alcohol consumption |
| COUGHING               | str     | Yes / No – persistent cough |
| SHORTNESS_OF_BREATH    | str     | Yes / No – dyspnea |
| SWALLOWING_DIFFICULTY  | str     | Yes / No – dysphagia |
| CHEST_PAIN             | str     | Yes / No – chest pain |
| LUNG_CANCER            | str     | YES / NO – confirmed lung cancer diagnosis |

## lung_cancer_patients_symptoms_2025.csv

**Rows**: 3,000 patients (PATIENT_ID 0–2999, 1:1 match with clinical features)

| Column              | Type    | Description |
|---------------------|---------|-------------|
| PATIENT_ID          | int     | Unique patient identifier (join key) |
| CANCER_STAGE        | str     | Stage_I, Stage_II, Stage_IIIA, Stage_IIIB, or Stage_IV |
| TUMOR_SIZE_CM       | float   | Tumor diameter in centimeters |
| DIAGNOSIS_DATE      | str     | Date of diagnosis (YYYY-MM-DD, range: 2024-01 to 2025-12) |
| TREATMENT_RECEIVED  | str     | Treatment administered (see values below) |

### TREATMENT_RECEIVED values

| Stage      | Possible treatments |
|------------|---------------------|
| Stage_I    | Surgery_Lobectomy, Surgery_Wedge_Resection, SBRT |
| Stage_II   | Surgery_Lobectomy+Chemo, Surgery_Pneumonectomy+Chemo, Chemo+Radiation |
| Stage_IIIA | Chemo+Radiation, Surgery+Chemo+Radiation, Immunotherapy+Chemo |
| Stage_IIIB | Chemo+Radiation, Immunotherapy+Chemo, Targeted_Therapy |
| Stage_IV   | Immunotherapy, Targeted_Therapy, Chemo+Immunotherapy, Palliative_Care |

## Data generation notes

- This is **mock / synthetic data** for demonstration purposes only.
- Cancer stage distribution is weighted: patients with LUNG_CANCER=YES are more
  likely to have advanced stages; LUNG_CANCER=NO patients are skewed toward Stage_I.
- Tumor size correlates with stage (higher stage → larger average tumor).
- The `TREATMENT_RECEIVED` column reflects plausible treatments for each stage
  based on NCCN guidelines (see `treatment-data/best_practices_lung_cancer.md`).
- Random seed: 42 (reproducible).
