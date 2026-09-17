# ClipScript — TikTok URL transcription API

A runnable Python backend and browser interface inspired by your screenshot. Paste a TikTok link, transcribe Thai/English/other supported speech, show segment timestamps, copy text, or export TXT/SRT. Upload a saved audio/video file when a link cannot be downloaded.

## What “free API token” means

- **Groq mode (default):** create your own key at https://console.groq.com/keys. Use a Free plan account. This application cannot issue a Groq key or guarantee an unlimited quota. Current documented Whisper free limits are 20 requests/minute, 2,000/day, 7,200 audio seconds/hour and 28,800/day, with a 25 MB upload limit. Your account's actual limits are authoritative; limits can change. If your account has paid billing enabled, API calls may cost money.
- **Local mode:** uses faster-whisper on your own CPU. No transcription API key or API charges. Hardware, electricity, and any server hosting remain your responsibility. A model downloads on first use; subsequent inference runs locally.
- **App access token:** a separate token protecting this API. Generated at startup for free, or set APP_API_TOKEN to keep a stable value. It is not a Groq credential.

The complete flow is: TikTok URL → yt-dlp downloads media → FFmpeg extracts audio → Groq or local Whisper → text and timestamps. Groq's audio URL parameter does not make an ordinary TikTok webpage a directly transcribable audio file.

## Start on your computer

Requires Python 3.10+ and FFmpeg (including ffprobe). On a supported macOS system with Homebrew:

```bash
brew install python ffmpeg
cd tiktok-transcriber
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` in your editor:

```dotenv
GROQ_API_KEY=your_actual_groq_key
TRANSCRIBER=groq
APP_API_TOKEN=
```

Run:

```bash
python app.py
```

1. Open http://127.0.0.1:8000 in your browser.
2. Copy the **App access token** printed in your terminal into the connection panel.
3. Click Connect, paste a TikTok video link, select Auto-detect or Thai/English, and click Transcribe video.

The key is read only by the backend. The browser token stays in page memory; refreshing requires re-entering it. Do not share the terminal token. Restarting generates a new token unless APP_API_TOKEN is set.

For older Macs that cannot install current Python or FFmpeg packages, run this backend on a supported Linux machine and open its interface through an SSH tunnel. This package has not been verified on macOS Catalina.

## Deploy 24/7 to Render.com (Docker)

To run the backend 24/7 in the cloud without keeping your local computer on:

1. Push this repository to GitHub (`sanwithz/TokScript`).
2. Go to [Render Dashboard](https://dashboard.render.com/web/new?onboarding=active).
3. Connect your GitHub repository **`sanwithz/TokScript`**.
4. Render automatically detects the **`Dockerfile`** (Runtime: **Docker**).
5. Choose Region: **Singapore** (fastest for Thailand / Asia) and Instance Type: **Free**.
6. Under **Environment Variables**, add:
   - `GROQ_API_KEY` = your Groq API key (`gsk_...`)
   - `APP_API_TOKEN` = your custom app access token (e.g. `1212312121`)
   - `TRANSCRIBER` = `groq`
7. Click **Deploy Web Service**.
8. Once deployed, Render will provide your public URL (e.g. `https://tokscript-backend.onrender.com`).
9. In your frontend (e.g. [TokScript on Vercel](https://tokscript-steel.vercel.app)), paste this Render URL into **Server URL** and enter your `APP_API_TOKEN` to connect!

## No API key: local Whisper mode

With the virtual environment activated:

```bash
python -m pip install faster-whisper
```

Set these values in `.env` and restart:

```dotenv
TRANSCRIBER=local
WHISPER_MODEL=small
```

The multilingual `small` model is the default. Larger models require more memory and processing time; `base` is lighter but may be less accurate. Local mode uses CPU INT8 and a maximum 15-minute inference deadline. Model downloads need an internet connection and cache space. Audio stays on your server in local mode. Groq mode sends extracted audio to Groq.

## Call your API

Replace YOUR_APP_TOKEN with the app token from your terminal, and paste a real public video URL:

```bash
curl http://127.0.0.1:8000/api/transcribe \
  -H 'Authorization: Bearer YOUR_APP_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.tiktok.com/@creator/video/VIDEO_ID","language":"auto"}'
```

Upload fallback sends raw file bytes, not multipart form data:

```bash
curl 'http://127.0.0.1:8000/api/transcribe-file?language=th' \
  -H 'Authorization: Bearer YOUR_APP_TOKEN' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary '@video.mp4'
```

Example response shape (illustrative, not a real transcript):

```json
{
  "title": "Example clip",
  "source_url": "https://www.tiktok.com/@creator/video/123",
  "language": "english",
  "duration": 4.2,
  "engine": "groq",
  "text": "Example spoken words.",
  "segments": [{"start": 0, "end": 4.2, "text": "Example spoken words."}],
  "srt": "1\n00:00:00,000 --> 00:00:04,200\nExample spoken words."
}
```

Interactive API documentation: http://127.0.0.1:8000/docs. Protected endpoints require the Authorization header; use the curl examples or browser app.

## Deployment and practical limits

- Runs locally by default, bound to 127.0.0.1. No public service was deployed and no external account or token was created.
- This is a single-user API, with one processing request at a time. The concurrency lock is per process: run **one worker only**. An occupied server returns HTTP 429. Requests wait until processing finishes; there is no persistent job queue.
- The Python downloader, FFmpeg, and optional Whisper worker need a normal computer/container/VPS. This backend is not a static Vercel page or an Apps Script deployment. A separate frontend can later be hosted elsewhere with appropriate authentication/CORS.
- For remote personal access, keep loopback binding and use an SSH tunnel: `ssh -L 8000:127.0.0.1:8000 user@your-server`. Open localhost:8000 on your computer. Public multi-user deployment needs HTTPS, per-user auth, stricter egress controls, a job queue, and quotas.
- Only TikTok URL input is supported. Short share links are resolved with redirects restricted to known TikTok hosts. The downloader uses only the TikTok extractor. TikTok can block public downloads; this is not an access-control bypass. Upload files you can access legitimately when a URL fails.
- Input max: 100 MB / 15 minutes. Download max time: 120 seconds. Remote transcription max timeout: 180 seconds. Local transcription max time: 900 seconds. A lost connection may leave a job running until its timeout; temporary files are cleaned after completion.
- No transcript history is stored. Copy or download your result before refreshing. Audio conversion and download errors produce readable messages.
- No translation, rewriting, or hook generation is included. The core task is transcription in the spoken language.

## Troubleshooting

| Symptom | Action |
|---|---|
| Invalid app token / 401 | Copy the current terminal token; a restart may change it. |
| Missing Groq key | Set GROQ_API_KEY in .env, then restart. |
| Groq limit reached / 429 | Wait for quota reset or switch to local mode. No automatic paid fallback. |
| TikTok video cannot download | Use the full video URL, update yt-dlp, or upload a saved MP4. |
| FFmpeg missing | Install FFmpeg and ensure both ffmpeg and ffprobe are on PATH. |
| No speech / poor accuracy | Check audio, choose its spoken language, try a clearer clip. |
| Local model fails | Verify faster-whisper, model download connectivity, and available memory. |

Update the downloader when TikTok changes:

```bash
python -m pip install --upgrade yt-dlp
```

## Validation

Run the included checks with `python -m unittest -v test_app.py`. Checks cover token enforcement, URL restrictions, controlled redirect handling, quota errors, subtitle formatting, file limits, temporary-file cleanup, and the API response flow using mocked provider/download operations. Real FFmpeg conversion is checked when installed. These checks do not prove that TikTok will allow downloads from your IP or validate real speech-recognition accuracy. Live Groq transcription requires your key; local inference requires downloading the optional model.

## Primary references

- Groq speech-to-text: https://console.groq.com/docs/speech-to-text
- Groq free-plan limits: https://console.groq.com/docs/rate-limits
- Groq API keys: https://console.groq.com/keys
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- faster-whisper: https://github.com/SYSTRAN/faster-whisper
