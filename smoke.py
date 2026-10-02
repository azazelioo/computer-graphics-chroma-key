"""Run inside the built container: docker compose exec -T web python < smoke.py."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
from PIL import Image
from server import app

with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=0x00ff00:s=160x120:r=30:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-vf','drawbox=x=50:y=30:w=60:h=60:color=red:t=fill',
                    '-c:v','libx264','-c:a','aac','-shortest',str(p/'in.mp4')],check=True)
    c=app.test_client()
    with (p/'in.mp4').open('rb') as f:
        r=c.post('/api/upload',data={'video':(f,'test.mp4')})
    assert r.status_code==200,r.json
    r=c.post('/api/render',json={'id':r.json['id'],'key':'#00ff00','background':'#ffffff','similarity':.15,'blend':.02,'spill':'green'})
    assert r.status_code==202,r.json
    job=r.json['job']
    for _ in range(120):
        state=c.get('/api/jobs/'+job).json
        if state['state'] in ('done','error'):break
        time.sleep(.5)
    assert state['state']=='done',state
    r=c.get(f'/api/jobs/{job}/video?download=1')
    assert r.status_code==200 and 'attachment' in r.headers['Content-Disposition']
    (p/'out.mp4').write_bytes(r.data)
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(p/'out.mp4')]))
    assert any(s['codec_type']=='audio' for s in info['streams'])
    assert abs(float(info['format']['duration'])-1)<.15
    subprocess.run(['ffmpeg','-v','error','-i',str(p/'out.mp4'),'-frames:v','1',str(p/'out.png')],check=True)
    with Image.open(p/'out.png') as im:
        assert min(im.getpixel((10,10))[:3])>235
        r,g,b=im.getpixel((80,60))[:3]
        assert r>220 and g<35 and b<35
    print('PASS: video upload, chromakey pixels, object preservation, audio, duration and MP4 download')
