# Study Brainrot Generator

Solo GitHub Actions pipeline: give it a study topic, get back a captioned MP4 as a build artifact.

## Setup
1. Repo secrets: `GEMINI_API_KEY` (required), `DEEPSEEK_API_KEY` (fallback, optional but recommended).
2. Drop a few royalty-free vertical loop clips (`.mp4`/`.mov`) into `assets/` (e.g. from Pixabay/Pexels).
3. Actions tab → "Generate Study Video" → Run workflow → enter a topic.
4. Download the MP4 from the run's artifacts.

## Pipeline
`research.py` (Wikipedia) → `script_gen.py` (Gemini, falls back to DeepSeek on rate-limit) → `voice_gen.py` (Edge-TTS + word timings) → `assemble.py` (FFmpeg captions + concat) → `build/output.mp4`.
