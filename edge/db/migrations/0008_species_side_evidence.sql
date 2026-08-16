-- 0008 -- evidence provenance for independent Stage 3 classifiers.
-- Keypoint quality is geometry evidence. Species and physical flank side are
-- separate model decisions and must stay separately reviewable.

ALTER TABLE detections ADD COLUMN species_model_version TEXT;

ALTER TABLE flank_crops ADD COLUMN side_confidence REAL;
ALTER TABLE flank_crops ADD COLUMN side_source TEXT;
ALTER TABLE flank_crops ADD COLUMN side_model_version TEXT;

CREATE INDEX ix_det_species_model ON detections(species, species_model_version)
  WHERE species IS NOT NULL;
CREATE INDEX ix_crops_side_evidence ON flank_crops(side, side_model_version);
