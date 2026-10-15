"""음성 창 나누기, 영상 장면 프레임, OCR (모델 없이). ffmpeg·OCR이 없으면 해당 테스트는 건너뛴다."""

import numpy as np
import pytest

from noseout import extractors, media, ocr

requires_ffmpeg = pytest.mark.skipif(media.find("ffmpeg") is None or media.find("ffprobe") is None,
                                     reason="ffmpeg not in vendor/ffmpeg")
requires_ocr = pytest.mark.skipif(not ocr.available(), reason="Windows OCR (winrt) not available")


def tone(seconds: float, amplitude: float = 8000, freq: float = 440) -> np.ndarray:
    t = np.arange(int(seconds * media.SAMPLE_RATE)) / media.SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


@requires_ffmpeg
def test_windows_of_long_audio(tmp_path):
    media.write_wav(tone(70), tmp_path / "a.wav")
    pieces, duration = media.audio_windows(str(tmp_path / "a.wav"), tmp_path)
    assert round(duration) == 70
    # 30초 창, 25초 간격: 0-30, 25-55, 그리고 50-70 꼬리(앞 창과 겹치지 않는 새 소리 15초 ≥ 2초)
    assert [(round(p.start), round(p.end)) for p in pieces] == [(0, 30), (25, 55), (50, 70)]
    assert all(media.probe(p.path)[0] <= 30.01 for p in pieces)


@requires_ffmpeg
def test_short_tail_and_short_file(tmp_path):
    media.write_wav(tone(31), tmp_path / "a.wav")             # 꼬리의 새 소리 1초 < 2초 → 버림
    pieces, _ = media.audio_windows(str(tmp_path / "a.wav"), tmp_path)
    assert [(round(p.start), round(p.end)) for p in pieces] == [(0, 30)]
    media.write_wav(tone(1.5), tmp_path / "b.wav")            # 30초보다 짧은 파일은 통째로 (2초 미만이면 버림)
    assert media.audio_windows(str(tmp_path / "b.wav"), tmp_path)[0] == []
    media.write_wav(tone(8), tmp_path / "c.wav")
    assert [(round(p.start), round(p.end)) for p in media.audio_windows(str(tmp_path / "c.wav"), tmp_path)[0]] == [(0, 8)]


@requires_ffmpeg
def test_silent_windows_are_skipped(tmp_path):
    quiet = np.random.default_rng(0).normal(0, 8, 40 * media.SAMPLE_RATE).astype(np.int16)
    loud = tone(30)
    media.write_wav(np.concatenate([quiet[: 30 * media.SAMPLE_RATE], loud]), tmp_path / "a.wav")
    pieces, _ = media.audio_windows(str(tmp_path / "a.wav"), tmp_path)
    assert pieces and all(p.start >= 25 for p in pieces)       # 앞의 무음 창(0-30)은 벡터를 만들지 않는다


@requires_ffmpeg
def test_compressed_audio_and_video_soundtrack(test_data, tmp_path):
    pieces, duration = media.audio_windows(str(test_data / "media" / "home_show_ep12.m4a"), tmp_path)
    assert 60 < duration < 70 and [round(p.start) for p in pieces] == [0, 25, 50]
    duration, has_audio, has_video = media.probe(str(test_data / "media" / "clip_2026-08-30.mp4"))
    assert (round(duration), has_audio, has_video) == (15, True, True)
    assert media.probe(str(test_data / "media" / "room_tone.wav"))[2] is False


@requires_ffmpeg
def test_keyframes_one_per_shot(test_data, tmp_path):
    frames, duration, has_audio = media.keyframes(str(test_data / "media" / "clip_2026-08-30.mp4"), tmp_path)
    # 5초씩 세 장면을 3초마다 뽑으면 5장인데, 같은 장면은 하나만 남는다
    assert len(frames) == 3 and has_audio
    starts = [f.start for f in frames]
    assert starts[0] < 5 <= starts[1] < 10 <= starts[2] + 1     # 장면 경계에서 3초 간격 이내
    assert [f.end for f in frames[:-1]] == starts[1:] and frames[-1].end == duration
    assert len(list(tmp_path.glob("frame-*.jpg"))) == 3         # 버린 프레임 파일은 지웠다


def test_average_hash_ignores_small_changes(tmp_path):
    from PIL import Image, ImageDraw

    a = Image.new("RGB", (640, 360), "white")
    ImageDraw.Draw(a).rectangle((0, 0, 320, 360), fill="black")
    b = a.copy()
    ImageDraw.Draw(b).point((600, 300), fill="black")           # 거의 같은 프레임
    c = Image.new("RGB", (640, 360), "white")
    ImageDraw.Draw(c).rectangle((0, 0, 640, 180), fill="black")  # 다른 장면
    for name, im in [("a", a), ("b", b), ("c", c)]:
        im.save(tmp_path / f"{name}.jpg")
    ha, hb, hc = (media.average_hash(tmp_path / f"{n}.jpg") for n in "abc")
    assert bin(ha ^ hb).count("1") <= media.HASH_BITS_SAME < bin(ha ^ hc).count("1")


def test_missing_ffmpeg_is_a_file_error(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "find", lambda tool: None)
    media.write_wav(tone(3), tmp_path / "a.wav")
    with pytest.raises(extractors.ExtractError, match="isn.t installed"):
        media.audio_windows(str(tmp_path / "a.wav"), tmp_path)


def test_hangul_share():
    assert ocr.hangul_share("엘리베이터 점검") == 1.0
    assert ocr.hangul_share("Download complete") == 0.0
    assert ocr.hangul_share("1234 !!") == 0.0


@requires_ocr
def test_ocr_reads_english_screenshot(test_data):
    text = ocr.text_of_image(str(test_data / "pictures" / "Screenshots" / "Screenshot 2026-10-01 093000.png"))
    assert "Payment declined" in text and "CARD" in text and "51" in text
    assert "DownIoad" not in text                               # 영어는 영어 인식기로 (한국어 인식기의 오독 없음)


@requires_ocr
def test_ocr_reads_korean_scan(test_data, tmp_path):
    pages, _, _ = extractors.pdf_pages(str(test_data / "documents" / "엘리베이터_점검_안내.pdf"), tmp_path)
    text = ocr.text_of_image(pages[0].image_path)
    assert "엘리베이터" in text and "점검" in text and "101" in text


@requires_ocr
def test_ocr_of_broken_file_is_empty(tmp_path):
    (tmp_path / "bad.png").write_bytes(b"not an image")
    assert ocr.text_of_image(str(tmp_path / "bad.png")) == ""
