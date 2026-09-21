# Engine

번들 Python 엔진이 들어가는 자리다 — **PyInstaller onedir 산물만** 여기 산다.

```
python3 scripts/build_engine.py     # 빌드·manifest·통합 (사람이 실행)
python3 scripts/build_engine.py --verify
```

이 디렉터리가 README만 있으면 번들 엔진은 아직 공급되지 않은 것이다 —
`EngineLaunchMode.resolve`는 그 상태를 `.development`로 되돌리고,
빈 엔진을 성공으로 가장하지 않는다. FFmpeg 계약(presets/ffmpeg-manifest.json)이
채워지지 않으면 통합도 닫혀 있다.
