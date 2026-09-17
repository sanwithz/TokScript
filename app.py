"""Single-user TikTok transcription API. Run: python app.py"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('APP_API_TOKEN') or secrets.token_urlsafe(32)
ENGINE = os.getenv('TRANSCRIBER', 'groq').lower()
MAX_BYTES = 100 * 1024 * 1024
MAX_SECONDS = 900
app = FastAPI(title='TokScript API', version='1.0.0')
busy = asyncio.Lock()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def auth(request: Request):
    supplied = request.headers.get('authorization', '')
    groq_key = os.getenv('GROQ_API_KEY', '')
    supplied_bytes = supplied.encode()
    is_app_token = secrets.compare_digest(supplied_bytes, ('Bearer ' + TOKEN).encode())
    is_groq_token = bool(groq_key) and secrets.compare_digest(supplied_bytes, ('Bearer ' + groq_key).encode())
    if not (is_app_token or is_groq_token):
        raise HTTPException(401, 'Enter your app access token from the server terminal.')


def ready():
    if ENGINE not in ('groq', 'local'):
        raise HTTPException(503, 'TRANSCRIBER must be groq or local.')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise HTTPException(503, 'Install FFmpeg on the server, then restart.')
    if ENGINE == 'groq' and not os.getenv('GROQ_API_KEY'):
        raise HTTPException(503, 'Add GROQ_API_KEY to the server .env file and restart, or use local mode.')
    if ENGINE == 'local' and importlib.util.find_spec('faster_whisper') is None:
        raise HTTPException(503, 'Local mode needs: pip install faster-whisper')


def validate_url(value: str) -> str:
    try:
        u = urlsplit(value.strip())
        if (u.scheme != 'https' or u.hostname not in {'www.tiktok.com', 'tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'}
                or u.username or u.password or u.port not in (None, 443)):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, 'Use an HTTPS TikTok video or share link.')
    return u.geturl()


def resolve_url(value: str) -> str:
    value = validate_url(value)
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            u = urlsplit(value)
            if re.fullmatch(r'/@[^/]+/video/\d+/?', u.path):
                return 'https://www.tiktok.com' + u.path.rstrip('/')
            response = client.get(value)
            if response.status_code in (301, 302, 303, 307, 308):
                value = validate_url(urljoin(value, response.headers.get('location', '')))
            else:
                break
    raise HTTPException(422, 'Could not resolve this share link. Paste the full @username/video link or upload the video.')


def run_process(args: list[str], folder: Path, timeout: int, error: str):
    # Poll both runtime and disk usage. Kill and reap on EVERY exit path.
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                raise HTTPException(504, 'Processing timed out. Try a shorter clip.')
            if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) > MAX_BYTES * 2:
                raise HTTPException(413, 'The media is too large. Upload a smaller clip.')
            time.sleep(0.1)
        if proc.returncode:
            raise HTTPException(422, error)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def download(url: str, folder: Path):
    canonical = resolve_url(url)
    run_process([sys.executable, str(ROOT / 'worker.py'), 'download', canonical, str(folder)], folder, 120,
                'TikTok could not provide this video. It may be private, removed, region restricted, or blocked. Upload a saved video instead.')
    info = json.loads((folder / 'media.json').read_text())
    media = Path(info['path'])
    if media.parent.resolve() != folder.resolve() or not media.is_file():
        raise HTTPException(422, 'No downloadable media was returned. Try uploading the video.')
    if media.stat().st_size > MAX_BYTES:
        raise HTTPException(413, 'Video exceeds 100 MB.')
    return media, info.get('title', 'TikTok video'), canonical


def convert(media: Path, folder: Path):
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                '-show_entries', 'format=duration', '-of', 'json', str(media)],
                               capture_output=True, text=True, timeout=20, check=True)
        duration = float(json.loads(probe.stdout)['format']['duration'])
        if not 0 < duration <= MAX_SECONDS:
            raise HTTPException(413, 'Use a video between 0 and 15 minutes long.')
    except (subprocess.SubprocessError, ValueError, KeyError):
        raise HTTPException(422, 'Cannot read this media. Try an MP4 video or MP3 audio file.')
    audio = folder / 'audio.wav'
    run_process(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                 '-i', str(media), '-t', str(MAX_SECONDS + 1), '-vn', '-ac', '1', '-ar', '16000',
                 '-c:a', 'pcm_s16le', '-y', str(audio)], folder, 120, 'Could not extract audio from this file.')
    # WAV > 25 MB is possible near the duration limit; compress losslessly for Groq.
    if ENGINE == 'groq' and audio.stat().st_size >= 24 * 1024 * 1024:
        flac = folder / 'audio.flac'
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(audio), '-y', str(flac)], folder, 60, 'Audio conversion failed.')
        audio = flac
    if ENGINE == 'groq' and audio.stat().st_size >= 25_000_000:
        raise HTTPException(413, 'Audio exceeds the free-tier upload size. Split the clip into shorter parts.')
    return audio, duration


def transcribe(audio: Path, language: str, folder: Path):
    if ENGINE == 'local':
        run_process([sys.executable, str(ROOT / 'worker.py'), 'transcribe', str(audio), str(folder), language],
                    folder, 900, 'Local Whisper failed. Check the model download, available memory, and faster-whisper installation.')
        return json.loads((folder / 'transcript.json').read_text())
    data = {'model': 'whisper-large-v3-turbo', 'response_format': 'verbose_json',
            'timestamp_granularities[]': 'segment', 'temperature': '0'}
    if language != 'auto':
        data['language'] = language
    with audio.open('rb') as f, httpx.Client(timeout=180, trust_env=False) as client:
        response = client.post('https://api.groq.com/openai/v1/audio/transcriptions',
                               headers={'Authorization': 'Bearer ' + os.environ['GROQ_API_KEY']},
                               files={'file': (audio.name, f, 'application/octet-stream')}, data=data)
    if response.status_code == 429:
        raise HTTPException(429, 'Groq free-tier limit reached. Wait before retrying or switch the server to local mode.')
    if response.status_code in (401, 403):
        raise HTTPException(503, 'Groq rejected the server API key. Check GROQ_API_KEY.')
    if response.is_error:
        raise HTTPException(502, 'The transcription provider could not process this audio. Try again later.')
    return response.json()


def stamp(seconds: float, separator=','):
    ms = max(0, round(float(seconds) * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}'


def result(raw, title, source, duration):
    segments = [{'start': float(s['start']), 'end': float(s['end']), 'text': str(s['text']).strip()}
                for s in raw.get('segments', []) if str(s.get('text', '')).strip()]
    srt = '\n\n'.join(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}" for i, s in enumerate(segments, 1))
    return {'title': title, 'source_url': source, 'language': raw.get('language', 'unknown'),
            'duration': duration, 'engine': ENGINE, 'text': str(raw.get('text', '')).strip(), 'segments': segments, 'srt': srt}


def process(folder, language, url=None, uploaded=None, title=None):
    try:
        if url:
            media, title, source = download(url, folder)
        else:
            media, source = uploaded, None
        audio, duration = convert(media, folder)
        return result(transcribe(audio, language, folder), title, source, duration)
    except httpx.TimeoutException:
        raise HTTPException(504, 'The remote service timed out. Retry later or upload the video.')
    except httpx.HTTPError:
        raise HTTPException(502, 'Could not reach TikTok or Groq. Check the server internet connection.')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, 'Processing failed. Check the server dependencies and retry with a saved video.')


class URLInput(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    language: str = Field(default='auto', pattern=r'^(auto|[a-z]{2})$')


@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/status', dependencies=[Depends(auth)])
def status():
    ready()
    return {'ready': True, 'engine': ENGINE, 'max_minutes': MAX_SECONDS // 60}


@app.post('/api/transcribe', dependencies=[Depends(auth)])
async def url_transcription(body: URLInput):
    ready()
    validate_url(body.url)
    if busy.locked():
        raise HTTPException(429, 'Another clip is processing. Try again when it finishes.')
    async with busy:
        # Keep cleanup inside the worker thread so cancelled requests cannot remove files mid-work.
        def work():
            with tempfile.TemporaryDirectory(prefix='clipscript-') as tmp:
                return process(Path(tmp), body.language, url=body.url)
        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


@app.post('/api/transcribe-file', dependencies=[Depends(auth)])
async def file_transcription(request: Request, language: str = 'auto'):
    ready()
    if not re.fullmatch(r'auto|[a-z]{2}', language):
        raise HTTPException(422, 'Use auto or a two-letter language code.')
    if busy.locked():
        raise HTTPException(429, 'Another clip is processing. Try again when it finishes.')
    async with busy:
        with tempfile.TemporaryDirectory(prefix='clipscript-') as tmp:
            folder = Path(tmp)
            media = folder / 'upload.media'
            total = 0
            with media.open('wb') as f:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, 'Upload must be under 100 MB.')
                    f.write(chunk)
            if not total:
                raise HTTPException(422, 'Choose a video or audio file first.')
            task = asyncio.create_task(asyncio.to_thread(process, folder, language, uploaded=media, title='Uploaded clip'))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise


class SummarizeInput(BaseModel):
    text: str = Field(min_length=5, max_length=100000)
    title: str = Field(default='Video transcript', max_length=500)
    language: str = Field(default='auto', max_length=20)


@app.post('/api/summarize', dependencies=[Depends(auth)])
def summarize(body: SummarizeInput):
    groq_key = os.getenv('GROQ_API_KEY')
    if not groq_key:
        raise HTTPException(503, 'Add GROQ_API_KEY to the server .env file to enable summarization.')
    model = os.getenv('GROQ_CHAT_MODEL', 'qwen/qwen3.8-27b')
    system_prompt = (
        "You are an expert video content summarizer. Analyze the transcript and provide a clean, high-quality summary formatted as:\n\n"
        "### Key Highlights\n"
        "- **Topic 1:** Concise explanation\n"
        "- **Topic 2:** Concise explanation\n"
        "- **Topic 3:** Concise explanation\n\n"
        "### Main Summary\n"
        "1 to 2 easy-to-read, well-structured paragraphs explaining the core content.\n\n"
        "Respond in the same primary language as the transcript (e.g. Thai if the transcript is Thai, English if English). "
        "Keep it concise, engaging, accurate, and avoid loose asterisks or clutter."
    )
    user_prompt = f"Title: {body.title}\n\nTranscript:\n{body.text}\n\nPlease summarize this video."
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': 'Bearer ' + groq_key},
                json={
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt}
                    ],
                    'temperature': 0.3
                }
            )
    except httpx.RequestError:
        raise HTTPException(502, 'Could not reach Groq API. Check your internet connection.')

    if response.status_code == 429:
        raise HTTPException(429, 'Groq rate limit reached. Please wait a moment before trying again.')
    if response.status_code in (401, 403):
        raise HTTPException(503, 'Groq rejected the server API key. Check GROQ_API_KEY.')
    if response.status_code != 200:
        raise HTTPException(502, f'Groq summarization failed (HTTP {response.status_code}).')

    try:
        result = response.json()
        summary = result['choices'][0]['message']['content'].strip()
        return {'summary': summary, 'model': model}
    except (KeyError, IndexError, ValueError):
        raise HTTPException(502, 'Received an invalid response format from Groq.')


if __name__ == '__main__':
    import uvicorn
    print('\nTokScript: http://127.0.0.1:8000')
    print('App access token (keep private): ' + TOKEN + '\n', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=8000)
