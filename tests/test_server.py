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


def probe(path):
    return json.loads(s.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)


@pytest.mark.parametrize('mode',['color','image'])
def test_frame_rate_preserved(client,tmp_path,mode):
    src=tmp_path/'rate.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00ff00:s=160x120:r=24000/1001:d=1','-c:v','libx264',str(src)])
    vid=upload(client,src)
    im=io.BytesIO();Image.new('RGB',(50,50),'white').save(im,'PNG');im.seek(0)
    client.post(f'/api/video/{vid}/background',data={'image':(im,'bg.png')})
    out=finish(client,dict(id=vid,mode=mode))
    assert probe(out)['streams'][0]['r_frame_rate']=='24000/1001'


def test_expired_result_returns_json(client,tmp_path):
    s.jobs['expired']={'state':'done','path':str(tmp_path/'gone.mp4')}
    r=client.get('/api/jobs/expired/video')
    assert r.status_code==404 and r.is_json


def test_audio_offset_preserved(client,tmp_path):
    src=tmp_path/'offset.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00ff00:s=160x120:r=30:d=2',
           '-itsoffset','0.5','-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac',str(src)])
    vid=upload(client,src);out=finish(client,dict(id=vid))
    # First 250 ms must remain silent instead of moving the delayed audio to t=0.
    raw=s.run(['ffmpeg','-v','error','-i',str(out),'-t','0.25','-map','0:a:0','-f','s16le','-ac','1','pipe:1']).stdout
    import array
    pcm=array.array('h',raw)
    assert not pcm or max(abs(x) for x in pcm)<100


@pytest.mark.parametrize('ext,codec',[('webm','libvpx-vp9'),('avi','mpeg4'),('mov','libx264')])
def test_other_video_containers(client,tmp_path,ext,codec):
    src=tmp_path/f'input.{ext}'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00ff00:s=160x120:r=24:d=0.5','-c:v',codec,str(src)])
    out=finish(client,dict(id=upload(client,src)))
    assert probe(out)['streams'][0]['codec_name']=='h264'


def test_portrait_rotation(client,tmp_path):
    src=tmp_path/'raw.mp4';fixture_video(src)
    rotated=tmp_path/'rotated.mp4'
    s.run(['ffmpeg','-v','error','-display_rotation','90','-i',str(src),'-c','copy',str(rotated)])
    vid=upload(client,rotated)
    meta=json.loads((s.folder(vid)/'meta.json').read_text())
    assert (meta['width'],meta['height'])==(120,160)
    out=finish(client,dict(id=vid))
    v=probe(out)['streams'][0]
    assert (v['width'],v['height'])==(120,160)


def test_bad_background_and_request_limits(client,tmp_path):
    src=tmp_path/'fixture.mp4';fixture_video(src);vid=upload(client,src)
    assert client.post(f'/api/video/{vid}/background',data={'image':(io.BytesIO(b'bad'),'bad.png')}).status_code==400
    assert client.post('/api/upload').status_code==400
    assert client.post('/api/render',data='bad',content_type='application/json').status_code==400
    with s.app.test_request_context():
        old=s.app.config['MAX_CONTENT_LENGTH']
        try:
            s.app.config['MAX_CONTENT_LENGTH']=10
            r=client.post('/api/upload',data={'video':(io.BytesIO(b'x'*20),'large.mp4')})
            assert r.status_code==413 and r.is_json
        finally:s.app.config['MAX_CONTENT_LENGTH']=old


def test_busy_and_worker_failure(client,tmp_path,monkeypatch):
    src=tmp_path/'fixture.mp4';fixture_video(src);vid=upload(client,src)
    s.jobs['busy']={'state':'running'}
    assert client.post('/api/render',json={'id':vid}).status_code==409
    s.jobs.clear()
    def fail(*args,**kwargs):raise subprocess.TimeoutExpired('ffmpeg',1200)
    monkeypatch.setattr(s,'run',fail)
    r=client.post('/api/render',json={'id':vid});job=r.json['job']
    for _ in range(100):
        if s.jobs[job]['state']=='error':break
        time.sleep(.01)
    assert s.jobs[job]['state']=='error'
    assert client.get(f'/api/jobs/{job}/video').status_code==404


def test_duration_limit_and_audio_only(client,tmp_path):
    src=tmp_path/'long.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=green:s=16x16:r=1/301','-frames:v','2','-c:v','libx264',str(src)])
    with src.open('rb') as f:
        r=client.post('/api/upload',data={'video':(f,'long.mp4')})
    assert r.status_code==400 and '5 минут' in r.json['error']
    audio=tmp_path/'audio.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.1','-c:a','aac',str(audio)])
    with audio.open('rb') as f:
        assert client.post('/api/upload',data={'video':(f,'audio.mp4')}).status_code==400


def test_odd_dimensions_and_range_download(client,tmp_path):
    src=tmp_path/'odd.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=s=161x121:r=30:d=0.2','-pix_fmt','yuv444p','-c:v','libx264',str(src)])
    out=finish(client,dict(id=upload(client,src)))
    v=probe(out)['streams'][0]
    assert (v['width'],v['height'])==(160,120)
    job=next(k for k,j in s.jobs.items() if j.get('path')==str(out))
    r=client.get(f'/api/jobs/{job}/video',headers={'Range':'bytes=0-99'})
    assert r.status_code==206 and len(r.data)==100


def test_anamorphic_aspect_ratio(client,tmp_path):
    src=tmp_path/'anamorphic.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00ff00:s=160x120:r=30:d=0.2','-vf','setsar=2/1','-c:v','libx264',str(src)])
    vid=upload(client,src)
    out=finish(client,dict(id=vid))
    v=probe(out)['streams'][0]
    assert (v['width'],v['height'])==(320,120)
    assert v['sample_aspect_ratio']=='1:1'


@pytest.mark.parametrize('parameter,low,high',[('similarity',0.001,0.2),('blend',0,0.3)])
def test_keying_controls_change_actual_pixels(client,tmp_path,parameter,low,high):
    src=tmp_path/'shades.mp4'
    s.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00cc00:s=160x120:r=30:d=0.2','-c:v','libx264',str(src)])
    vid=upload(client,src);pixels=[]
    for i,value in enumerate((low,high)):
        data=dict(id=vid,key='#00ff00',background='#ffffff',similarity=.001,blend=0)
        data[parameter]=value
        out=finish(client,data)
        img=tmp_path/f'pixel{i}.png'
        s.run(['ffmpeg','-v','error','-i',str(out),'-frames:v','1',str(img)])
        with Image.open(img) as im:pixels.append(im.getpixel((10,10)))
    assert sum(abs(a-b) for a,b in zip(*pixels))>100
