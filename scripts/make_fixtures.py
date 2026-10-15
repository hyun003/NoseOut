"""교차 모달 검증용 합성 입력을 tests/fixtures에 만든다. 개인 파일은 쓰지 않는다.

- 이미지: Pillow로 그린 장면(도형, 차트, 텍스트가 든 문서·표지판)
- 음성: Windows 내장 TTS(System.Speech)로 읽은 영어·한국어 문장, 16 kHz mono WAV

usage: .venv\\Scripts\\python scripts\\make_fixtures.py
"""

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FONT = "C:/Windows/Fonts/arialbd.ttf"


def font(size):
    return ImageFont.truetype(FONT, size)


def red_circle():
    im = Image.new("RGB", (800, 800), "white")
    ImageDraw.Draw(im).ellipse((150, 150, 650, 650), fill=(220, 30, 30))
    return im


def bar_chart():
    im = Image.new("RGB", (1000, 700), "white")
    d = ImageDraw.Draw(im)
    d.line((100, 600, 940, 600), fill="black", width=4)
    d.line((100, 80, 100, 600), fill="black", width=4)
    for i, h in enumerate([180, 320, 260, 450, 390]):
        x = 160 + i * 150
        d.rectangle((x, 600 - h, x + 90, 600), fill=(40, 110, 200))
    return im


def stop_sign():
    im = Image.new("RGB", (800, 800), (150, 190, 230))
    d = ImageDraw.Draw(im)
    import math
    pts = [(400 + 300 * math.cos(math.radians(22.5 + 45 * k)), 400 + 300 * math.sin(math.radians(22.5 + 45 * k)))
           for k in range(8)]
    d.polygon(pts, fill=(200, 20, 30), outline="white", width=14)
    d.text((400, 400), "STOP", fill="white", font=font(150), anchor="mm")
    return im


def invoice():
    im = Image.new("RGB", (850, 1100), "white")
    d = ImageDraw.Draw(im)
    d.text((60, 60), "INVOICE", fill="black", font=font(64))
    d.text((60, 160), "Invoice No. 4821     Date: 2026-09-14", fill="black", font=font(26))
    y = 260
    for item, price in [("Web hosting (12 months)", "$480.00"), ("Domain renewal", "$20.00"),
                        ("Support plan", "$750.00")]:
        d.text((60, y), item, fill="black", font=font(26))
        d.text((640, y), price, fill="black", font=font(26))
        y += 60
    d.line((60, y + 10, 790, y + 10), fill="black", width=3)
    d.text((60, y + 40), "TOTAL DUE", fill="black", font=font(34))
    d.text((600, y + 40), "$1,250.00", fill="black", font=font(34))
    return im


def night_sky():
    im = Image.new("RGB", (1200, 800), (10, 15, 40))
    d = ImageDraw.Draw(im)
    import random
    rng = random.Random(7)
    for _ in range(300):
        x, y = rng.randrange(1200), rng.randrange(800)
        r = rng.choice([1, 1, 2])
        d.ellipse((x - r, y - r, x + r, y + r), fill="white")
    d.ellipse((900, 100, 1050, 250), fill=(245, 240, 210))
    d.ellipse((940, 90, 1080, 230), fill=(10, 15, 40))  # 초승달
    return im


IMAGES = {"red_circle": red_circle, "bar_chart": bar_chart, "stop_sign": stop_sign,
          "invoice": invoice, "night_sky": night_sky}

SPEECH = {
    "meeting_en": ("en-US", "The team meeting has been moved to Friday afternoon at three o'clock."),
    "weather_en": ("en-US", "Tomorrow it will rain heavily in the morning, so bring an umbrella."),
    "recipe_en": ("en-US", "Boil the pasta for ten minutes, then add the tomato sauce and fresh basil."),
    "train_ko": ("ko-KR", "서울역에서 부산으로 가는 기차는 오후 두 시에 출발합니다."),
}

TTS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq $env:TTS_CULTURE } | Select-Object -First 1
if ($voice) { $s.SelectVoice($voice.VoiceInfo.Name) } else { Write-Error "no voice for $env:TTS_CULTURE"; exit 3 }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:TTS_OUT, $fmt)
$s.Speak($env:TTS_TEXT)
$s.Dispose()
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, draw in IMAGES.items():
        draw().save(OUT / f"{name}.png")
    made = {}
    for name, (culture, text) in SPEECH.items():
        path = OUT / f"{name}.wav"
        env = {**__import__("os").environ, "TTS_CULTURE": culture, "TTS_OUT": str(path), "TTS_TEXT": text}
        result = subprocess.run(["powershell", "-NoProfile", "-Command", TTS], env=env, capture_output=True, text=True)
        if result.returncode == 0 and path.exists():
            made[name] = {"culture": culture, "text": text}
        else:
            print(f"skip {name}: {result.stderr.strip()[:200]}")
    (OUT / "speech.json").write_text(json.dumps(made, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"images: {len(IMAGES)}, speech: {len(made)} -> {OUT}")


if __name__ == "__main__":
    main()
