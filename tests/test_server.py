import importlib.util
import io
import json
from pathlib import Path
import subprocess
import time

from PIL import Image
import pytest

spec = importlib.util.spec_from_file_location('chroma', Path(__file__).parents[1] / 'server.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(s, 'ROOT', tmp_path)
    s.jobs.clear()
    return s.app.test_client()


def fixture_video(path, key='0x00ff00'):
    s.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color={key}:s=160x120:r=10:d=1',
           '-f','lavfi','-i','sine=frequency=440:duration=1','-vf','drawbox=x=50:y=30:w=60:h=60:color=red:t=fill',
           '-c:v','libx264','-c:a','aac','-pix_fmt','yuv420p','-shortest',str(path)])


def upload(client, path):
    with path.open('rb') as f:
        r=client.post('/api/upload',data={'video':(f,path.name)})
    assert r.status_code==200, r.json
    return r.json['id']


def finish(client, data):
    r=client.post('/api/render',json=data)
    assert r.status_code==202, r.json
    ident=r.json['job']
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        result=client.get('/api/jobs/'+ident).json
        if result['state'] in ('done','error'):break
        time.sleep(.1)
    assert result['state']=='done', result
    r=client.get(f'/api/jobs/{ident}/video?download=1')
    assert r.status_code==200
    assert 'attachment' in r.headers['Content-Disposition']
    return Path(s.jobs[ident]['path'])


@pytest.mark.parametrize('key',['#00ff00','#0000ff'])
def test_color_replacement_pixels_audio_and_download(client,tmp_path,key):
    src=tmp_path/'fixture.mp4';fixture_video(src,'0x'+key[1:]);vid=upload(client,src)
    out=finish(client,dict(id=vid,key=key,background='#ffffff',similarity=.15,blend=.02,spill='green' if key=='#00ff00' else 'blue'))
    frame=tmp_path/'check.png';s.run(['ffmpeg','-v','error','-i',str(out),'-frames:v','1',str(frame)])
    with Image.open(frame) as im:
        assert min(im.getpixel((10,10))[:3])>235
        r,g,b=im.getpixel((80,60))[:3];assert r>220 and g<35 and b<35
    info=json.loads(s.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]).stdout)
    assert any(x['codec_type']=='audio' for x in info['streams'])
    assert abs(float(info['format']['duration'])-1)<.15


def test_image_background(client,tmp_path):
    src=tmp_path/'fixture.mp4';fixture_video(src);vid=upload(client,src)
    im=io.BytesIO();Image.new('RGB',(90,200),'blue').save(im,'PNG');im.seek(0)
    assert client.post(f'/api/video/{vid}/background',data={'image':(im,'bg.png')}).status_code==200
    out=finish(client,dict(id=vid,mode='image',similarity=.15,blend=.02,preview=True))
    frame=tmp_path/'image-check.png';s.run(['ffmpeg','-v','error','-i',str(out),'-frames:v','1',str(frame)])
    with Image.open(frame) as im:
        r,g,b=im.getpixel((10,10))[:3];assert b>230 and r<30 and g<30


def test_validation(client,tmp_path):
    assert client.post('/api/upload',data={'video':(io.BytesIO(b'broken'),'bad.mp4')}).status_code==400
    assert client.post('/api/render',json=[]).status_code==400
    assert client.get('/api/video/invalid/frame').status_code==400
    src=tmp_path/'fixture.mp4';fixture_video(src);vid=upload(client,src)
    for params in ({'key':'red;bad'},{'similarity':float('nan')},{'blend':2},{'mode':'bad'},{'mode':'image'},{'preview':'false'}):
        assert client.post('/api/render',json={'id':vid,**params}).status_code==400
    assert client.get('/api/jobs/unknown/video').status_code==404


def test_preview_duration(client,tmp_path):
    src=tmp_path/'long.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=green:s=160x120:r=10:d=6','-c:v','libx264',str(src)])
    vid=upload(client,src)
    out=finish(client,dict(id=vid,preview=True))
    info=json.loads(s.run(['ffprobe','-v','error','-show_format','-of','json',str(out)]).stdout)
    assert abs(float(info['format']['duration'])-5)<.1
