"""Isolated workers allow download and local inference deadlines to be enforced."""
import json
import os
from pathlib import Path
import sys


def main():
    action, source, directory = sys.argv[1:4]
    folder = Path(directory)
    if action == 'download':
        import yt_dlp

        def limit(info, *, incomplete=False):
            duration = info.get('duration')
            if duration is not None and duration > 900:
                return 'Video exceeds 15 minutes.'

        options = {'format': 'bestaudio/best', 'outtmpl': str(folder / 'video.%(ext)s'),
                   'noplaylist': True, 'quiet': True, 'no_warnings': True,
                   'max_filesize': 100 * 1024 * 1024, 'socket_timeout': 20, 'retries': 1,
                   'fragment_retries': 1, 'match_filter': limit,
                   'allowed_extractors': ['TikTok'], 'cachedir': False}
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source, download=True)
            if not info or info.get('_type') == 'playlist':
                raise ValueError('Expected one video.')
            path = ydl.prepare_filename(info)
            if not Path(path).is_file():
                raise ValueError('Video was not downloaded.')
            (folder / 'media.json').write_text(json.dumps({'path': path, 'title': info.get('title', 'TikTok video')}, ensure_ascii=False))
    elif action == 'transcribe':
        from faster_whisper import WhisperModel
        model = WhisperModel(os.getenv('WHISPER_MODEL', 'small'), device='cpu', compute_type='int8')
        language = sys.argv[4]
        segments, info = model.transcribe(source, language=None if language == 'auto' else language, vad_filter=True, beam_size=5)
        parts = [{'start': s.start, 'end': s.end, 'text': s.text.strip()} for s in segments]
        (folder / 'transcript.json').write_text(json.dumps({'text': ' '.join(s['text'] for s in parts), 'language': info.language, 'segments': parts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
