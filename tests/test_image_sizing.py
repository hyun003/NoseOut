import pytest
from PIL import Image

from noseout import image_sizing


@pytest.mark.parametrize("budget", image_sizing.BUDGETS)
@pytest.mark.parametrize("size", [(900, 1440), (768, 1024), (4032, 3024), (100, 100), (48, 2000)])
def test_target_fits_budget(size, budget):
    target = image_sizing.target_size(*size, budget)
    assert target is not None
    h, w = target
    assert h % 48 == 0 and w % 48 == 0
    assert (h // 48) * (w // 48) <= budget


def test_very_thin_gets_one_row():
    # 원본(Swift)과 같은 동작: 한 변이 0이 되면 48px로 두고 다른 변을 최대 길이로 자른다
    assert image_sizing.target_size(1, 100000, 70) == (48, 70 * 48)


def test_prepare_outputs_png_at_target(tmp_path):
    path = tmp_path / "a.jpg"
    Image.new("RGB", (1024, 768), (10, 20, 30)).save(path)
    png = image_sizing.prepare(str(path), 140)
    import io

    out = Image.open(io.BytesIO(png))
    assert out.format == "PNG"
    assert (out.height, out.width) == image_sizing.target_size(768, 1024, 140)
