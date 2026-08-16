# PUGMARK 500-IMAGE MASTER TEST BENCHMARK

Total Images: **360**

## Category Breakdown

- **1_TIGER_PHOTOS**: `80` photos (ATRW Wild Tigers, Left & Right Flanks, Repeat Identifiers)
- **2_OTHER_ANIMALS**: `140` photos (Deer, Coyote, Bobcat, Raccoon, Cattle, Birds)
- **3_BLANK_FRAMES**: `140` photos (Empty foliage, night IR triggers)
- **4_HUMANS_AND_VEHICLES**: `0` photos (Field rangers, forest vehicles for Privacy Redaction)
- **STATION_P01_MIXED_BURST**: Camera trap memory card simulation folder (for Import Photos scan)
- **STATION_P02_TIGER_TRAIL**: Camera trap trail simulation folder

## Ground Truth Samples

| Filename | Category | Ground Truth Entity | Expected AI Behavior |
| :--- | :--- | :--- | :--- |
| `TIGER_REC_01_P01_003597.jpg` | tiger | IND-TIGER-250 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_01_P02_002499.jpg` | tiger | IND-TIGER-250 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_01_P03_000152.jpg` | tiger | IND-TIGER-250 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_01_P04_001510.jpg` | tiger | IND-TIGER-250 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_02_P01_003523.jpg` | tiger | IND-TIGER-256 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_02_P02_001291.jpg` | tiger | IND-TIGER-256 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_02_P03_003970.jpg` | tiger | IND-TIGER-256 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_02_P04_004946.jpg` | tiger | IND-TIGER-256 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_03_P01_003900.jpg` | tiger | IND-TIGER-171 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_03_P02_003062.jpg` | tiger | IND-TIGER-171 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_03_P03_000053.jpg` | tiger | IND-TIGER-171 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_03_P04_000920.jpg` | tiger | IND-TIGER-171 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_04_P01_002636.jpg` | tiger | IND-TIGER-247 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_04_P02_004485.jpg` | tiger | IND-TIGER-247 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_04_P03_004023.jpg` | tiger | IND-TIGER-247 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_04_P04_004132.jpg` | tiger | IND-TIGER-247 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_05_P01_002249.jpg` | tiger | IND-TIGER-238 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_05_P02_001276.jpg` | tiger | IND-TIGER-238 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_05_P03_003611.jpg` | tiger | IND-TIGER-238 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_05_P04_004355.jpg` | tiger | IND-TIGER-238 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_06_P01_003370.jpg` | tiger | IND-TIGER-264 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_06_P02_003551.jpg` | tiger | IND-TIGER-264 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_06_P03_000634.jpg` | tiger | IND-TIGER-264 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_06_P04_002883.jpg` | tiger | IND-TIGER-264 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_07_P01_001260.jpg` | tiger | IND-TIGER-54 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_07_P02_003849.jpg` | tiger | IND-TIGER-54 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_07_P03_001659.jpg` | tiger | IND-TIGER-54 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_07_P04_003486.jpg` | tiger | IND-TIGER-54 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_08_P01_001015.jpg` | tiger | IND-TIGER-237 | Identify -> Stripe Match -> Confirmed Entity |
| `TIGER_REC_08_P02_005005.jpg` | tiger | IND-TIGER-237 | Identify -> Stripe Match -> Confirmed Entity |

*(See `manifest.csv` for all 360 entries)*
