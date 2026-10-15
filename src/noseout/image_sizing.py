"""원본 DigUp(Sources/LlamaRuntime/ImageSizing.swift)과 같은 방식으로 이미지를 비전 인코더 크기에 맞춘다.

mtmd는 범위를 벗어난 이미지만 다시 줄이지만, 기준 구현(transformers Gemma 4 image processor)은 항상 토큰 예산을
꽉 채운다. 그래서 여기서 미리 크기를 맞추고, llama-server는 `--image-min-tokens 1 --image-max-tokens 1120`으로
받은 크기 그대로 쓰게 한다. 원본의 Swift 리사이저는 Pillow BICUBIC을 비트 단위로 옮긴 것이므로 Pillow를 그대로 쓴다.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

BUDGETS = (70, 140, 280, 560, 1120)
PATCH = 16
POOLING = 3


def target_size(height: int, width: int, budget: int) -> tuple[int, int] | None:
    """(height, width): `budget`개의 soft token(각 3×3 패치, 16px)을 채우도록 비율을 유지하며 48px 배수로 내림."""
    max_patches = budget * POOLING * POOLING
    factor = ((max_patches * PATCH * PATCH) / (height * width)) ** 0.5
    side = POOLING * PATCH
    target_h = int(factor * height / side) * side
    target_w = int(factor * width / side) * side
    if target_h == 0 and target_w == 0:
        return None
    max_side = (max_patches // (POOLING * POOLING)) * side
    if target_h == 0:
        target_h = side
        target_w = min(width // height * side, max_side)
    elif target_w == 0:
        target_w = side
        target_h = min(height // width * side, max_side)
    return target_h, target_w


def prepare(path: str, budget: int) -> bytes:
    """이미지를 RGB로 읽어 EXIF 방향을 적용하고 `budget`에 맞게 줄인 뒤 무손실 PNG 바이트로 돌려준다."""
    if budget not in BUDGETS:
        raise ValueError(f"budget must be one of {BUDGETS}")
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        size = target_size(image.height, image.width, budget)
        if size is None:
            raise ValueError(f"image too thin to embed: {path}")
        if size != (image.height, image.width):
            image = image.resize((size[1], size[0]), Image.Resampling.BICUBIC)
        out = io.BytesIO()
        image.save(out, format="PNG", compress_level=1)
        return out.getvalue()
