"""The no-rectification fallback, measured -- AUDIT_AND_REVISED_PLAN.md
Task 4: "measure a no-rectification fallback with heavy augmentation, so
we know what we lose if the keypoint model underperforms."

    python -m tools.train_identify_no_rect [--epochs N] [--smoke-test]

Same TriHard batch-hard training as tools/train_identify.py -- same
entities, same held-out-identity split, same 40 epochs by default -- so
the comparison in docs/RESULTS.md is apples to apples. The only
difference is the crop: no rectify_flank(), no keypoints at all, just
the raw image resized straight to 256x128, with heavy augmentation
(random rotation, scale/crop jitter, colour jitter, horizontal flip)
standing in for the geometric normalisation rectification would
otherwise provide.

Horizontal flip is fine here specifically because there is no
rectification step to make "which side" meaningful in the first place --
this path does not distinguish near/far side at all, so flipping does
not invalidate anything the way it would for the keypoint-based crop.

Writes edge/models/identify/identify_embedder_no_rect.pt (gitignored) -- a
second, separate model, not a replacement for the rectified one.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2                              # noqa: E402
import numpy as np                      # noqa: E402
import torch                            # noqa: E402
import torchvision.transforms as T      # noqa: E402

from edge import config                                            # noqa: E402
from edge.pipeline.identify import (                              # noqa: E402
    IMAGENET_MEAN, IMAGENET_STD, RECT_HEIGHT, RECT_WIDTH, TripletEmbedder)
from tools.atrw_dataset import held_out_identity_split, load_labelled  # noqa: E402
from tools.train_identify import (                                # noqa: E402
    K_IMAGES, LR, MARGIN, P_IDENTITIES, batch_hard_triplet_loss, build_entities, sample_batch)

WEIGHTS_PATH = config.local_model_path("identify/identify_embedder_no_rect.pt")

AUGMENT = T.Compose([
    T.RandomRotation(20),
    T.RandomResizedCrop((RECT_HEIGHT, RECT_WIDTH), scale=(0.75, 1.0), ratio=(1.8, 2.2)),
    T.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2),
    T.RandomHorizontalFlip(p=0.5),
])


def load_no_rect(row: dict) -> torch.Tensor | None:
    img = cv2.imread(row["orig_path"])
    if img is None:
        return None
    resized = cv2.resize(img, (RECT_WIDTH, RECT_HEIGHT))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    pil = T.functional.to_pil_image(rgb)
    augmented = AUGMENT(pil)
    arr = np.asarray(augmented).astype(np.float32) / 255.0
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    return torch.from_numpy(arr.transpose(2, 0, 1)).float()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--smoke-test", action="store_true")
    args = ap.parse_args()
    epochs = 2 if args.smoke_test else args.epochs

    rows = load_labelled("train")
    train_rows, held_rows = held_out_identity_split(rows)
    train_entities = build_entities(train_rows)
    print(f"train entities: {len(train_entities)} "
          f"({sum(len(v) for v in train_entities.values())} images)")

    # Training only -- see tools/train_identify.py's identical comment.
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"training device: {device}"
          + (f" ({torch.cuda.get_device_name(0)})" if device.type == "cuda" else ""))

    backbone = config.RESNET50_BACKBONE_PATH if config.RESNET50_BACKBONE_PATH.exists() else None
    if backbone is None:
        print("no local ImageNet backbone supplied; training from scratch (no download attempted)")
    model = TripletEmbedder(backbone_weights=backbone).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    rng = random.Random(20260813)

    steps_per_epoch = max(1, sum(len(v) for v in train_entities.values()) // (P_IDENTITIES * K_IMAGES))
    print(f"training (no rectification, heavy augmentation): {epochs} epochs x "
          f"{steps_per_epoch} steps, P={P_IDENTITIES} K={K_IMAGES} margin={MARGIN}")

    model.train()
    for epoch in range(epochs):
        epoch_loss, epoch_active = 0.0, 0
        t0 = time.time()
        for step in range(steps_per_epoch):
            rows_b, labels = sample_batch(train_entities, P_IDENTITIES, K_IMAGES, rng)
            tensors = [load_no_rect(r) for r in rows_b]
            keep = [(t, y) for t, y in zip(tensors, labels) if t is not None]
            if len(keep) < 4:
                continue
            x = torch.stack([t for t, _ in keep]).to(device)
            y = torch.tensor([lbl for _, lbl in keep], device=device)

            optimizer.zero_grad()
            emb = model(x)
            loss, n_active = batch_hard_triplet_loss(emb, y)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            epoch_active += n_active
        print(f"  epoch {epoch+1}/{epochs}: mean loss {epoch_loss/steps_per_epoch:.4f}, "
              f"{epoch_active} active triplets, {time.time()-t0:.1f}s")

    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), WEIGHTS_PATH)
    print(f"saved {WEIGHTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
