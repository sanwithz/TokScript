import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from fastapi import HTTPException
from fastapi.testclient import TestClient
import app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.headers = {'Authorization': 'Bearer ' + app.TOKEN}

    def test_auth_and_home(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/health').json()['status'], 'ok')
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        with patch.dict(os.environ, {'GROQ_API_KEY': ''}), patch.object(app, 'ENGINE', 'groq'):
            self.assertEqual(self.client.get('/api/status', headers=self.headers).status_code, 503)

    def test_url_restrictions(self):
        for value in ['http://www.tiktok.com/@x/video/123', 'https://tiktok.com.evil.test/x',
                      'https://127.0.0.1/x', 'file:///etc/passwd', 'https://x@www.tiktok.com/x',
                      'https://www.tiktok.com:8000/x', 'https://www.tiktok.com:bad/x']:
            with self.subTest(value=value), self.assertRaises(HTTPException):
                app.validate_url(value)
        self.assertEqual(app.resolve_url('https://www.tiktok.com/@x/video/123?track=a'),
                         'https://www.tiktok.com/@x/video/123')
        redirect = Mock(status_code=302, headers={'location': 'https://127.0.0.1/private'})
        with patch('app.httpx.Client') as client:
            client.return_value.__enter__.return_value.get.return_value = redirect
            with self.assertRaises(HTTPException):
                app.resolve_url('https://vm.tiktok.com/abc/')

    def test_subtitle_rounding(self):
        self.assertEqual(app.stamp(59.9999), '00:01:00,000')
        r = app.result({'text': 'สวัสดี', 'segments': [{'start': 0, 'end': 1.5, 'text': 'สวัสดี'}]}, 'Thai', None, 1.5)
        self.assertEqual(r['srt'], '1\n00:00:00,000 --> 00:00:01,500\nสวัสดี')

    def test_api_flow_and_cleanup(self):
        seen = []
        def downloaded(url, folder):
            seen.append(folder)
            f = folder / 'video.mp4'
            f.write_bytes(b'mocked video')
            return f, 'Example', url
        with patch.object(app, 'ready'), patch.object(app, 'download', side_effect=downloaded), \
             patch.object(app, 'convert', side_effect=lambda media, folder: (media, 2)), \
             patch.object(app, 'transcribe', return_value={'text': 'Hello', 'language': 'en', 'segments': [{'start': 0, 'end': 2, 'text': 'Hello'}]}):
            response = self.client.post('/api/transcribe', headers=self.headers, json={'url': 'https://www.tiktok.com/@x/video/123'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['text'], 'Hello')
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())

    def test_upload_limits(self):
        with patch.object(app, 'ready'), patch.object(app, 'MAX_BYTES', 3):
            self.assertEqual(self.client.post('/api/transcribe-file', headers=self.headers, content=b'1234').status_code, 413)
            self.assertEqual(self.client.post('/api/transcribe-file', headers=self.headers, content=b'').status_code, 422)
            self.assertEqual(self.client.post('/api/transcribe-file?language=bad', headers=self.headers, content=b'12').status_code, 422)

    def test_groq_quota_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GROQ_API_KEY': 'fake-test-key'}), \
             patch.object(app, 'ENGINE', 'groq'), patch('app.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = Mock(status_code=429)
            f = Path(tmp) / 'audio.wav'
            f.write_bytes(b'fake')
            with self.assertRaises(HTTPException) as error:
                app.transcribe(f, 'auto', Path(tmp))
            self.assertEqual(error.exception.status_code, 429)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg not installed')
    def test_real_audio_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            f = folder / 'input.wav'
            with wave.open(str(f), 'wb') as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(16000)
                writer.writeframes(b'\0\0' * 16000)
            audio, duration = app.convert(f, folder)
            self.assertAlmostEqual(duration, 1)
            self.assertTrue(audio.is_file())

    def test_summarize(self):
        # 401 without auth
        self.assertEqual(self.client.post('/api/summarize', json={'text': 'Hello world test'}).status_code, 401)
        # 200 with auth and mocked Groq response
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': 'Summary bullets\n- item 1'}}]
        }
        with patch('app.httpx.Client') as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_resp
            res = self.client.post('/api/summarize', headers=self.headers,
                                   json={'text': 'This is a sample transcript to summarize.', 'title': 'Test'})
            self.assertEqual(res.status_code, 200)
            self.assertIn('summary', res.json())
            self.assertEqual(res.json()['summary'], 'Summary bullets\n- item 1')


if __name__ == '__main__':
    unittest.main()
