"""Trains the TriHard triplet-loss embedder (docs/DATA.md §2,
docs/MODEL_CHOICES.md) on ATRW's labelled training images.

    python -m tools.train_identify [--epochs N] [--smoke-test]

Writes edge/models/identify/identify_embedder.pt (gitignored -- see
.gitignore), the path the field runtime loads by absolute local path.
Held-out-by-identity split (tools/atrw_dataset.held_out_identity_split):
held-out identities never appear in a training batch. Rectification
uses each image's own ATRW ground-truth keypoints
(edge/pipeline/identify.py::rectify_flank) -- production inference on a
real Pench crop with no keypoints is a separate, documented gap
(docs/MODEL_CHOICES.md), not something this training script papers over.

PK batch sampling + batch-hard (TriHard) mining, exactly the paper's
baseline: P entities per batch, K images each: every anchor is scored
against its hardest positive (max distance, same entity) and hardest
negative (min distance, different entity) within the batch, per Hermans
et al.'s "In Defense of the Triplet Loss," which the ATRW paper cites
for TriHard. CPU-only (torch>=2.2 cpu wheel per requirements.txt) --
training is offline, not something the range-office laptop ever does.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2                     # noqa: E402
import torch                   # noqa: E402
import torch.nn.functional as F  # noqa: E402

from edge import config                                          # noqa: E402
from edge.pipeline.identify import (                              # noqa: E402
    WEIGHTS_PATH, TripletEmbedder, infer_side, preprocess, rectify_flank)
from tools.atrw_dataset import held_out_identity_split, load_labelled  # noqa: E402
MARGIN = 0.3
P_IDENTITIES = 8
K_IMAGES = 4
LR = 3e-4


def build_entities(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Groups rows into (ind_id, side) entities, side inferred per crop
    (docs/DATA.md §1 -- the entity, not the individual, is the unit of
    re-identification). Only entities with >=2 images can ever supply an
    anchor+positive pair."""
    by_entity: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        side = infer_side(r["keypoints"])
        if side:
            by_entity[(r["ind_id"], side)].append(r)
    return {k: v for k, v in by_entity.items() if len(v) >= 2}


def load_rectified(row: dict, cfg: config.Identify) -> torch.Tensor | None:
    img = cv2.imread(row["orig_path"])
    if img is None:
        return None
    side = infer_side(row["keypoints"])
    rect = rectify_flank(img, row["keypoints"], side, cfg)
    if rect is None:
        return None
    return preprocess(rect)


def sample_batch(entities: dict[tuple[str, str], list[dict]], p: int, k: int,
                  rng: random.Random) -> tuple[list[dict], list[int]]:
    keys = rng.sample(list(entities), min(p, len(entities)))
    rows, labels = [], []
    for i, key in enumerate(keys):
        pool = entities[key]
        chosen = rng.choices(pool, k=k) if len(pool) < k else rng.sample(pool, k)
        rows.extend(chosen)
        labels.extend([i] * len(chosen))
    return rows, labels


def batch_hard_triplet_loss(embeddings: torch.Tensor, labels: torch.Tensor,
                             margin: float = MARGIN) -> tuple[torch.Tensor, int]:
    """TriHard / batch-hard mining (Hermans et al.). embeddings are
    L2-normalised, so squared Euclidean distance is a monotonic
    transform of cosine similarity: d^2 = 2 - 2*cos."""
    dist = torch.cdist(embeddings, embeddings, p=2)
    same = labels.unsqueeze(0) == labels.unsqueeze(1)
    diff = ~same
    eye = torch.eye(len(labels), dtype=torch.bool, device=embeddings.device)
    same_no_self = same & ~eye

    losses = []
    for i in range(len(labels)):
        pos_d = dist[i][same_no_self[i]]
        neg_d = dist[i][diff[i]]
        if pos_d.numel() == 0 or neg_d.numel() == 0:
            continue
        hardest_pos = pos_d.max()
        hardest_neg = neg_d.min()
        losses.append(F.relu(hardest_pos - hardest_neg + margin))
    if not losses:
        return torch.tensor(0.0, requires_grad=True), 0
    stacked = torch.stack(losses)
    return stacked.mean(), int((stacked > 0).sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--smoke-test", action="store_true",
                     help="2 tiny epochs, to prove the loop runs before a long background run")
    args = ap.parse_args()
    epochs = 2 if args.smoke_test else args.epochs

    cfg = config.CONFIG.identify
    rows = load_labelled("train")
    train_rows, held_rows = held_out_identity_split(rows)
    train_entities = build_entities(train_rows)
    held_entities = build_entities(held_rows)
    print(f"train entities: {len(train_entities)} ({sum(len(v) for v in train_entities.values())} "
          f"images) -- held-out entities: {len(held_entities)} "
          f"({sum(len(v) for v in held_entities.values())} images)")

    print("pre-rectifying and caching tensors (one-time cost) ...")
    t0 = time.time()
    cache: dict[str, torch.Tensor] = {}
    for key, entity_rows in train_entities.items():
        for row in entity_rows:
            if row["file_name"] in cache:
                continue
            t = load_rectified(row, cfg)
            if t is not None:
                cache[row["file_name"]] = t
    train_entities = {k: [r for r in v if r["file_name"] in cache]
                       for k, v in train_entities.items()}
    train_entities = {k: v for k, v in train_entities.items() if len(v) >= 2}
    print(f"  {len(cache)} crops rectified in {time.time()-t0:.1f}s; "
          f"{len(train_entities)} entities usable")

    # Training only -- production inference (edge/pipeline/identify.py) stays
    # CPU-only on purpose, matching the range-office laptop deployment target
    # (CLAUDE.md). This script runs offline, on whatever hardware is building
    # the release, so it is free to use a GPU when one is actually there.
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
    print(f"training: {epochs} epochs x {steps_per_epoch} steps, "
          f"P={P_IDENTITIES} K={K_IMAGES} margin={MARGIN}")

    model.train()
    for epoch in range(epochs):
        epoch_loss, epoch_active = 0.0, 0
        t0 = time.time()
        for step in range(steps_per_epoch):
            rows_b, labels = sample_batch(train_entities, P_IDENTITIES, K_IMAGES, rng)
            x = torch.stack([cache[r["file_name"]] for r in rows_b]).to(device)
            y = torch.tensor(labels, device=device)

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
