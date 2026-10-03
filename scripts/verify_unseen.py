"""Real HTTP verification on newly generated videos, independent of course dataset."""
import json
import os
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import uuid
from PIL import Image

base = os.environ.get('BASE_URL', 'http://127.0.0.1:8004')
def get(path):
    return urllib.request.urlopen(base+path,timeout=60)
def post_file(path, field, filename, blob):
    boundary=uuid.uuid4().hex
    body=(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+blob+f'\r\n--{boundary}--\r\n'.encode())
    return json.load(urllib.request.urlopen(urllib.request.Request(base+path,data=body,headers={'Content-Type':f'multipart/form-data; boundary={boundary}'}),timeout=60))
def run(args):
    return subprocess.run(args,capture_output=True,check=True,timeout=90).stdout
assert json.load(get('/health'))['status']=='ok'
with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)
    for i,key in enumerate(('#00ff00','#0000ff')):
        source=p/f'new-{i}.mov'
        run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=0x{key[1:]}:s=240x180:r=24:d=1.25',
             '-f','lavfi','-i','sine=frequency=523:duration=1.25','-vf','drawbox=x=80:y=50:w=80:h=80:color=red:t=fill',
             '-c:v','libx264','-c:a','aac','-shortest',str(source)])
        data=post_file('/api/upload','video',source.name,source.read_bytes())
        settings={'id':data['id'],'key':key,'background':'#ffffff','similarity':.15,'blend':.02}
        if i:
            image=BytesIO();Image.new('RGB',(120,100),'white').save(image,'PNG')
            post_file('/api/video/'+data['id']+'/background','image','new-background.png',image.getvalue())
            settings['mode']='image'
        request=urllib.request.Request(base+'/api/render',data=json.dumps(settings).encode(),headers={'Content-Type':'application/json'})
        job=json.load(urllib.request.urlopen(request,timeout=30))['job']
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            status=json.load(get('/api/jobs/'+job))
            if status['state'] in ('done','error'):break
            time.sleep(.25)
        assert status['state']=='done',status
        response=get('/api/jobs/'+job+'/video?download=1')
        assert 'attachment' in response.headers['Content-Disposition']
        out=p/f'out-{i}.mp4';out.write_bytes(response.read())
        info=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        assert (video['width'],video['height'],video['r_frame_rate'])==(240,180,'24/1')
        assert any(s['codec_type']=='audio' for s in info['streams'])
        assert abs(float(info['format']['duration'])-1.25)<.1
        png=p/f'frame-{i}.png';run(['ffmpeg','-v','error','-i',str(out),'-frames:v','1',str(png)])
        with Image.open(png) as im:
            assert min(im.getpixel((10,10))[:3])>235
            r,g,b=im.getpixel((120,90))[:3];assert r>220 and g<35 and b<35
print('PASS: 2 new MOV videos over HTTP; green/blue keys, color/image backgrounds, object pixels, 24 fps, audio, duration, downloaded MP4')
