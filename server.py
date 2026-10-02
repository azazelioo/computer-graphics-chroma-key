"""Local chroma-key editor. FFmpeg processes media; Flask serves the UI."""
import json
import math
import os
from pathlib import Path
from fractions import Fraction
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(os.environ.get('DATA_DIR', '/tmp/kg-chroma-key'))
ROOT.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_url_path='', static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024
pool = ThreadPoolExecutor(max_workers=1)
lock = threading.Lock()
jobs = {}


def run(args, timeout=60):
    return subprocess.run(args, capture_output=True, check=True, timeout=timeout)


def folder(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Некорректный идентификатор видео.')
    p = ROOT / value
    if not (p / 'meta.json').exists():
        raise ValueError('Видео не найдено. Загрузите его заново.')
    return p


def color(value):
    if not isinstance(value, str) or not re.fullmatch('#[0-9a-fA-F]{6}', value):
        raise ValueError('Цвет должен иметь формат #RRGGBB.')
    return '0x' + value[1:]


def number(data, key, low, high, default):
    try:
        value = float(data.get(key, default))
    except (ValueError, TypeError):
        raise ValueError('Некорректный параметр: ' + key)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Параметр вне диапазона: ' + key)
    return value


@app.errorhandler(413)
def too_large(_):
    return jsonify(error='Файл превышает 256 МБ.'), 413


@app.errorhandler(ValueError)
def invalid(exc):
    return jsonify(error=str(exc)), 400


@app.get('/')
def index():
    return app.send_static_file('index.html')


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/api/upload')
def upload():
    import shutil
    # Bound retained disk usage. Files older than a day expire between uploads.
    for old in ROOT.iterdir():
        if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
            shutil.rmtree(old, ignore_errors=True)
    if shutil.disk_usage(ROOT).free < 1024 ** 3:
        raise ValueError('Недостаточно места для видео. Освободите диск.')
    source = request.files.get('video')
    if not source:
        raise ValueError('Выберите видео.')
    ident = uuid.uuid4().hex
    p = ROOT / ident
    p.mkdir()
    try:
        source.save(p / 'source')
        info = json.loads(run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_streams', '-show_format', '-of', 'json', str(p / 'source')]).stdout)
        stream = next(s for s in info['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        if not set(info['format']['format_name'].split(',')) & {'mov', 'mp4', 'matroska', 'webm', 'avi', 'mpeg', 'mpegts'}:
            raise ValueError('Поддерживаются MP4, MOV, WebM, MKV, AVI и MPEG.')
        duration = float(stream.get('duration') or info['format']['duration'])
        try:
            fps = Fraction(stream.get('avg_frame_rate', '0/1'))
        except (ValueError, ZeroDivisionError):
            fps = Fraction(30)
        if not 1 <= fps <= 120:
            fps = Fraction(30)
        audio = next((x for x in info['streams'] if x['codec_type'] == 'audio'), None)
        audio_offset = float(audio.get('start_time', 0)) - float(stream.get('start_time', 0)) if audio else 0
        w, h = stream['width'], stream['height']
        if not math.isfinite(duration) or not 0 < duration <= 300 or w * h > 3840 * 2160:
            raise ValueError('Лимиты: 5 минут, разрешение до 3840×2160.')
        sar = Fraction(stream.get('sample_aspect_ratio', '1:1').replace(':', '/')) if stream.get('sample_aspect_ratio') not in (None, 'N/A', '0:1') else Fraction(1)
        if not 0.25 <= sar <= 4 or w * h * max(1, float(sar)) > 3840 * 2160:
            raise ValueError('Слишком большой размер кадра с учётом пропорций пикселя.')
        # An auto-oriented frame defines display geometry, including phone rotation.
        run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(p / 'source'), '-map', f"0:{stream['index']}", '-vf', 'scale=trunc(iw*sar/2)*2:ih,setsar=1', '-frames:v', '1', str(p / 'frame.png')])
        with Image.open(p / 'frame.png') as frame:
            w, h = frame.size
        meta = dict(id=ident, width=w, height=h, duration=duration, name=source.filename, fps=str(fps), video_index=stream['index'], audio_offset=audio_offset)
        (p / 'meta.json').write_text(json.dumps(meta))
        return meta
    except (subprocess.SubprocessError, KeyError, StopIteration, OSError):
        shutil.rmtree(p, ignore_errors=True)
        raise ValueError('Не удалось прочитать видео. Используйте MP4, MOV или WebM.')
    except ValueError:
        shutil.rmtree(p, ignore_errors=True)
        raise


@app.get('/api/video/<ident>/frame')
def frame(ident):
    return send_file(folder(ident) / 'frame.png')


@app.post('/api/video/<ident>/background')
def background(ident):
    p = folder(ident)
    source = request.files.get('image')
    if not source:
        raise ValueError('Выберите изображение.')
    try:
        with Image.open(source.stream) as im:
            if im.width * im.height > 40_000_000:
                raise ValueError('Изображение больше 40 мегапикселей.')
            im = ImageOps.exif_transpose(im).convert('RGB')
            im.save(p / 'background.png')
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('Не удалось прочитать изображение.')
    return {'ok': True}


def command(p, data, out):
    meta = json.loads((p / 'meta.json').read_text())
    key = color(data.get('key', '#00ff00'))
    bg = color(data.get('background', '#284b70'))
    similarity = number(data, 'similarity', .001, 1, .045)
    blend = number(data, 'blend', 0, 1, .01)
    mode = data.get('mode', 'color')
    if mode not in ('color', 'image'):
        raise ValueError('Неизвестный тип фона.')
    if mode == 'image' and not (p / 'background.png').exists():
        raise ValueError('Сначала загрузите фоновое изображение.')
    spill = data.get('spill', 'off')
    if spill not in ('off', 'green', 'blue'):
        raise ValueError('Неизвестный режим подавления отблеска.')
    despill = f',format=rgba,despill=type={spill}:mix=0.5' if spill != 'off' else ''
    preview = data.get('preview', False)
    if not isinstance(preview, bool):
        raise ValueError('Некорректный режим предпросмотра.')
    duration = min(meta['duration'], 5) if preview else meta['duration']
    ratio = min(1, 960 / max(meta['width'], meta['height'])) if preview else 1
    w, h = max(2, int(meta['width'] * ratio) // 2 * 2), max(2, int(meta['height'] * ratio) // 2 * 2)
    fps = meta.get('fps', '30')
    offset = meta.get('audio_offset', 0)
    audio_filter = 'asetpts=PTS-STARTPTS'
    if offset > 0:
        audio_filter += f',adelay={offset * 1000}:all=1'
    elif offset < 0:
        audio_filter = f'asetpts=PTS-STARTPTS,atrim=start={-offset},asetpts=PTS-STARTPTS'
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '2', '-protocol_whitelist', 'file,pipe', '-i', str(p / 'source')]
    if mode == 'image':
        args += ['-loop', '1', '-framerate', fps, '-i', str(p / 'background.png')]
    else:
        args += ['-f', 'lavfi', '-i', f'color=c={bg}:s={w}x{h}:r={fps}']
    graph = (f"[0:{meta.get('video_index', 0)}]setpts=PTS-STARTPTS,scale={w}:{h},setsar=1,format=yuva444p,"
             f'chromakey=color={key}:similarity={similarity}:blend={blend}{despill}[fg];'
             f'[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[bg];'
             '[bg][fg]overlay=shortest=1:format=auto,format=yuv420p[out]')
    args += ['-filter_complex_threads', '1', '-filter_complex', graph, '-map', '[out]', '-map', '0:a:0?', '-af', audio_filter,
             '-t', str(duration), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2', '-c:a', 'aac', '-b:a', '192k',
             '-movflags', '+faststart', '-progress', 'pipe:1', str(out)]
    return args, duration


def render(ident, args, duration):
    job = jobs[ident]
    job['state'] = 'running'
    try:
        # A hard deadline bounds malformed inputs and pathological encodes.
        run(args, timeout=1200)
        job.update(state='done', progress=100)
    except subprocess.TimeoutExpired:
        job.update(state='error', error='Обработка заняла больше 20 минут. Используйте более короткое видео.')
    except subprocess.CalledProcessError as exc:
        app.logger.error('FFmpeg: %s', exc.stderr.decode(errors='replace')[-3000:])
        job.update(state='error', error='FFmpeg не смог обработать видео. Попробуйте другой файл или параметры.')
    except OSError:
        job.update(state='error', error='FFmpeg недоступен или закончилось место на диске.')


@app.post('/api/render')
def start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Ожидались параметры обработки.')
    p = folder(data.get('id'))
    ident = uuid.uuid4().hex
    out = p / f'{ident}.mp4'
    args, duration = command(p, data, out)
    os.utime(p, None)
    with lock:
        if any(j['state'] in ('queued', 'running') for j in jobs.values()):
            return jsonify(error='Уже обрабатывается видео. Дождитесь завершения.'), 409
        jobs[ident] = dict(state='queued', progress=0, path=str(out))
    pool.submit(render, ident, args, duration)
    return {'job': ident}, 202


@app.get('/api/jobs/<ident>')
def status(ident):
    job = jobs.get(ident)
    if not job:
        return jsonify(error='Задание не найдено.'), 404
    return {k: v for k, v in job.items() if k != 'path'}


@app.get('/api/jobs/<ident>/video')
def result(ident):
    job = jobs.get(ident)
    if not job or job['state'] != 'done':
        return jsonify(error='Видео ещё не готово.'), 404
    if not Path(job['path']).is_file():
        return jsonify(error='Срок хранения результата истёк. Обработайте видео заново.'), 404
    return send_file(job['path'], mimetype='video/mp4', as_attachment='download' in request.args, download_name='chroma-key.mp4', conditional=True)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8004, threaded=True)
