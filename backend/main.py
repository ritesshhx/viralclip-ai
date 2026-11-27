import os, re, json, uuid, zipfile, sqlite3, subprocess, time, secrets, socket
from pathlib import Path
from urllib.parse import urlparse, urlencode
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
load_dotenv()
BASE=Path(__file__).resolve().parent
DATA=Path(os.getenv('DATA_DIR',BASE/'data')); DATA.mkdir(parents=True,exist_ok=True)
JOBS=DATA/'jobs'; JOBS.mkdir(exist_ok=True); DB=DATA/'jobs.sqlite3'
MAX_MB=int(os.getenv('MAX_UPLOAD_MB','1500')); ALLOWED={x.strip().lower() for x in os.getenv('ALLOWED_SOURCE_HOSTS','').split(',') if x.strip()}
CID=os.getenv('GOOGLE_CLIENT_ID',''); CSECRET=os.getenv('GOOGLE_CLIENT_SECRET',''); REDIRECT=os.getenv('GOOGLE_REDIRECT_URI','http://localhost:8000/auth/google/callback')
app=FastAPI(title='ViralClip AI API',version='0.2.0')
app.add_middleware(SessionMiddleware,secret_key=os.getenv('SESSION_SECRET','change-me'),same_site='lax',https_only=os.getenv('COOKIE_HTTPS_ONLY','false').lower()=='true')
app.add_middleware(CORSMiddleware,allow_origins=os.getenv('FRONTEND_ORIGINS','http://localhost:8000').split(','),allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
with conn() as c: c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,progress INTEGER,message TEXT,source_name TEXT,settings TEXT,clips TEXT,created REAL,error TEXT)')
def update(jid,**kw):
 with conn() as c:
  r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
  if not r:return
  d=dict(r); d.update(kw); c.execute('UPDATE jobs SET status=?,progress=?,message=?,source_name=?,settings=?,clips=?,created=?,error=? WHERE id=?',(d['status'],d['progress'],d['message'],d['source_name'],d['settings'],d['clips'],d['created'],d['error'],jid))
def newjob(settings,name):
 jid=uuid.uuid4().hex[:12]
 with conn() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,'queued',0,'Waiting to start',name,json.dumps(settings),'[]',time.time(),None))
 return jid
def run(cmd):
 p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 if p.returncode:raise RuntimeError((p.stderr or p.stdout)[-2000:])
 return p.stdout
def duration(path):return float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)]).strip())
def drive_token(req):
 t=req.session.get('google')
 if not t or not t.get('access_token'):raise HTTPException(401,'Connect Google Drive first at /auth/google.')
 return t['access_token']
def cloud_validate(url):
 p=urlparse(url)
 if p.scheme!='https' or not p.hostname or p.hostname.lower() not in ALLOWED:raise HTTPException(400,'Cloud host is not allowlisted. Add its exact hostname to ALLOWED_SOURCE_HOSTS.')
 try:
  ip=socket.gethostbyname(p.hostname)
  if ip.startswith(('127.','10.','192.168.','169.254.')) or ip=='0.0.0.0':raise HTTPException(400,'Private network addresses are blocked.')
 except socket.gaierror:raise HTTPException(400,'Could not resolve cloud host.')
async def cloud_download(url,dest):
 cloud_validate(url); size=0
 async with httpx.AsyncClient(follow_redirects=False,timeout=120) as client:
  async with client.stream('GET',url) as r:
   if r.status_code!=200:raise RuntimeError(f'Cloud download returned HTTP {r.status_code}.')
   if 'text/html' in r.headers.get('content-type',''):raise RuntimeError('This is a sharing page, not a direct signed media URL.')
   with open(dest,'wb') as f:
    async for b in r.aiter_bytes():
     size+=len(b)
     if size>MAX_MB*1024*1024:raise RuntimeError('Source exceeds configured size limit.')
     f.write(b)
def drive_download(token,file_id,dest):
 with httpx.Client(timeout=180,follow_redirects=True) as client:
  h={'Authorization':'Bearer '+token}; m=client.get(f'https://www.googleapis.com/drive/v3/files/{file_id}',headers=h,params={'fields':'id,name,mimeType,size'})
  if m.status_code!=200:raise RuntimeError('Google Drive lookup failed; check file access.')
  meta=m.json()
  if not meta.get('mimeType','').startswith('video/'):raise RuntimeError('Choose a video file in Google Drive.')
  if int(meta.get('size') or 0)>MAX_MB*1024*1024:raise RuntimeError('Drive file exceeds configured size limit.')
  with client.stream('GET',f'https://www.googleapis.com/drive/v3/files/{file_id}',headers=h,params={'alt':'media'}) as r:
   if r.status_code!=200:raise RuntimeError('Drive download failed; check download permission.')
   with open(dest,'wb') as f:
    for b in r.iter_bytes():f.write(b)
  return meta.get('name','Google Drive video')
  
def youtube_download(url, dest):
    p = urlparse(url)

    allowed_hosts = (
        'youtube.com',
        'www.youtube.com',
        'm.youtube.com',
        'youtu.be',
        'www.youtube-nocookie.com'
    )

    if p.scheme != 'https' or p.hostname not in allowed_hosts:
        raise RuntimeError('Please provide a valid YouTube URL.')

    dest = str(dest)

    run([
        'python', '-m', 'yt_dlp',
     '--verbose',
'--remote-components', 'ejs:github',
        '--no-playlist',
        '--no-warnings',
        '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '--merge-output-format', 'mp4',
        '--max-filesize', f'{MAX_MB}M',
        '-o', dest,
        url
    ])

    if not os.path.exists(dest):
        raise RuntimeError('YouTube video download failed.')

    return 'YouTube video'
 
HOOK=re.compile(r'\b(wow|wait|what|no way|insane|crazy|unbelievable|oh my god|lets go|finally|secret|never|best|worst|amazing|clutch|fail|win|impossible|bro|watch this|actually)\b',re.I)
def transcribe(path,language):
 from faster_whisper import WhisperModel
 model=WhisperModel(os.getenv('WHISPER_MODEL','tiny'),device='cpu',compute_type='int8')
 segs,_=model.transcribe(str(path),language=None if language in ('','Auto detect',None) else language,vad_filter=True)
 return [{'start':float(s.start),'end':float(s.end),'text':s.text.strip()} for s in segs]
def select_clips(segs,total,count,length):
 if not segs:return [{'start':round(i*max(0,total-length)/max(1,count-1),2),'title':f'Moment {i+1}','score':0} for i in range(count)]
 ranked=[]
 for s in segs:
  st=max(0,min(max(0,total-length),(s['start']+s['end']-length)/2)); text=' '.join(x['text'] for x in segs if x['end']>=st and x['start']<=st+length)
  words=len(text.split()); score=len(HOOK.findall(text))*12+text.count('!')*2+text.count('?')+min(words/length,3)*5
  ranked.append({'start':round(st,2),'title':(text[:75]+'…') if len(text)>75 else text or 'Highlight','score':round(score,1)})
 ranked.sort(key=lambda x:x['score'],reverse=True); picked=[]
 for x in ranked:
  if all(abs(x['start']-p['start'])>length*.75 for p in picked):picked.append(x)
  if len(picked)>=count:break
 for i in range(count-len(picked)):
  st=max(0,min(total-length,(i+1)*(total-length)/(count-len(picked)+1)))
  if all(abs(st-p['start'])>length*.5 for p in picked):picked.append({'start':round(st,2),'title':'Suggested moment','score':0})
 return sorted(picked[:count],key=lambda x:x['start'])
def process(jid,source,name,settings):
 work=JOBS/jid; work.mkdir(parents=True,exist_ok=True)
 try:
  update(jid,status='processing',progress=5,message='Checking source video…',source_name=name); total=duration(source)
  if total<10:raise RuntimeError('Video must be longer than 10 seconds.')
  length=min(settings['duration'],int(total)); update(jid,progress=15,message='Transcribing with local Whisper tiny model…')
  segs=transcribe(source,settings.get('language')); update(jid,progress=35,message='Ranking candidate moments…')
  moments=select_clips(segs,total,settings['count'],length); clips=[]
  for i,m in enumerate(moments):
   filename=f'clip_{i+1:02d}.mp4'; out=work/filename; start=m['start']
   if settings['aspect']=='9:16':vf='scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920'
   elif settings['aspect']=='1:1':vf='scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080'
   else:vf='scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2'
   run(['ffmpeg','-y','-ss',str(start),'-i',str(source),'-t',str(min(settings['duration'],total-start)),'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','128k','-movflags','+faststart',str(out)])
   clips.append({**m,'id':i+1,'file':filename,'approved':False,'preview':f'/api/jobs/{jid}/clips/{filename}'})
   update(jid,progress=40+int(55*(i+1)/len(moments)),message=f'Rendering clip {i+1} of {len(moments)}…',clips=json.dumps(clips))
  with zipfile.ZipFile(work/'viralclip-ai-clips.zip','w',zipfile.ZIP_DEFLATED) as z:
   for x in clips:z.write(work/x['file'],x['file'])
  update(jid,status='ready',progress=100,message=f'{len(clips)} clips ready for review.',clips=json.dumps(clips))
 except Exception as e:update(jid,status='failed',progress=0,message='Processing failed.',error=str(e))
@app.get('/health')
def health():return {'ok':True,'service':'ViralClip AI API'}
@app.get('/auth/google')
def google_auth(req:Request):
 if not CID or not CSECRET:raise HTTPException(503,'Google OAuth credentials are not configured on the server.')
 state=secrets.token_urlsafe(24);req.session['oauth_state']=state
 q=urlencode({'client_id':CID,'redirect_uri':REDIRECT,'response_type':'code','scope':'https://www.googleapis.com/auth/drive.readonly','access_type':'offline','prompt':'consent','state':state})
 return RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?'+q)
@app.get('/auth/google/callback')
async def google_callback(req:Request,code:str,state:str):
 if not secrets.compare_digest(state,req.session.pop('oauth_state','')):raise HTTPException(400,'Invalid OAuth state.')
 async with httpx.AsyncClient() as c:r=await c.post('https://oauth2.googleapis.com/token',data={'code':code,'client_id':CID,'client_secret':CSECRET,'redirect_uri':REDIRECT,'grant_type':'authorization_code'})
 if r.status_code!=200:raise HTTPException(400,'Google sign-in failed.')
 req.session['google']=r.json();return RedirectResponse(os.getenv('FRONTEND_URL','/')+'?google=connected')
@app.get('/api/drive/videos')
async def drive_videos(req:Request):
 token=drive_token(req)
 async with httpx.AsyncClient() as c:r=await c.get('https://www.googleapis.com/drive/v3/files',headers={'Authorization':'Bearer '+token},params={'q':"trashed=false and mimeType contains 'video/'",'pageSize':50,'orderBy':'modifiedTime desc','fields':'files(id,name,mimeType,size,modifiedTime)'})
 if r.status_code!=200:raise HTTPException(400,'Could not list Drive videos.')
 return r.json().get('files',[])
def settings(count,duration,aspect,quality,captions,language):
 if aspect not in ('9:16','1:1','16:9'):raise HTTPException(400,'Invalid aspect ratio.')
 return {'count':count,'duration':duration,'aspect':aspect,'quality':quality,'captions':captions,'language':language}
@app.post('/api/jobs/drive')
def from_drive(req:Request,bg:BackgroundTasks,file_id:str=Form(...),count:int=Form(20),duration:int=Form(30),aspect:str=Form('9:16'),quality:str=Form('1080'),captions:bool=Form(True),language:str=Form('Auto detect')):
 token=drive_token(req);s=settings(count,duration,aspect,quality,captions,language);jid=newjob(s,'Google Drive video')
 def task():
  path=JOBS/jid/'source_video';path.parent.mkdir(parents=True,exist_ok=True)
  try:name=drive_download(token,file_id,path);process(jid,path,name,s)
  except Exception as e:update(jid,status='failed',message='Could not import Drive video.',error=str(e))
 bg.add_task(task);return {'job_id':jid,'status':'queued'}
@app.post('/api/jobs/cloud')
async def from_cloud(bg:BackgroundTasks,url:str=Form(...),count:int=Form(20),duration:int=Form(30),aspect:str=Form('9:16'),quality:str=Form('1080'),captions:bool=Form(True),language:str=Form('Auto detect')):
 cloud_validate(url);s=settings(count,duration,aspect,quality,captions,language);jid=newjob(s,'Authorized cloud storage')
 async def task():
  path=JOBS/jid/'source_video';path.parent.mkdir(parents=True,exist_ok=True)
  try:await cloud_download(url,path);process(jid,path,'Authorized cloud storage',s)
  except Exception as e:update(jid,status='failed',message='Could not import cloud video.',error=str(e))
 bg.add_task(task);return {'job_id':jid,'status':'queued'}
@app.post('/api/jobs/upload')
async def upload(bg:BackgroundTasks,file:UploadFile=File(...),count:int=Form(20),duration:int=Form(30),aspect:str=Form('9:16'),quality:str=Form('1080'),captions:bool=Form(True),language:str=Form('Auto detect')):
 if not file.filename.lower().endswith(('.mp4','.mov','.mkv','.webm')):raise HTTPException(400,'Upload MP4, MOV, MKV or WebM.')
 s=settings(count,duration,aspect,quality,captions,language);jid=newjob(s,file.filename);path=JOBS/jid/'source_video';path.parent.mkdir(parents=True,exist_ok=True);size=0
 with open(path,'wb') as f:
  while chunk:=await file.read(1024*1024):
   size+=len(chunk)
   if size>MAX_MB*1024*1024:raise HTTPException(413,'Video exceeds upload size limit.')
   f.write(chunk)
 bg.add_task(process,jid,path,file.filename,s);return {'job_id':jid,'status':'queued'}
 
@app.post('/api/jobs/youtube')
def from_youtube(
    bg: BackgroundTasks,
    url: str = Form(...),
    count: int = Form(20),
    duration: int = Form(30),
    aspect: str = Form('9:16'),
    quality: str = Form('1080'),
    captions: bool = Form(True),
    language: str = Form('Auto detect')
):
    s = settings(count, duration, aspect, quality, captions, language)
    jid = newjob(s, 'YouTube video')

    def task():
        path = JOBS / jid / 'source_video.mp4'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            name = youtube_download(url, path)
            process(jid, path, name, s)
        except Exception as e:
            update(
                jid,
                status='failed',
                message='YouTube download or processing failed.',
                error=str(e)
            )

    bg.add_task(task)
    return {'job_id': jid, 'status': 'queued'}
 
@app.get('/api/jobs/{jid}')
def status(jid:str):
 with conn() as c:r=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
 if not r:raise HTTPException(404,'Job not found')
 d=dict(r);return {k:d[k] for k in ('id','status','progress','message','source_name','error','clips')}
@app.get('/api/jobs/{jid}/clips/{filename}')
def clip(jid:str,filename:str):
 if not re.fullmatch(r'clip_\d{2}\.mp4',filename):raise HTTPException(400,'Invalid clip name')
 p=JOBS/jid/filename
 if not p.exists():raise HTTPException(404,'Clip not ready')
 return FileResponse(p,media_type='video/mp4',filename=filename)
@app.get('/api/jobs/{jid}/download')
def download(jid:str):
 p=JOBS/jid/'viralclip-ai-clips.zip'
 if not p.exists():raise HTTPException(404,'ZIP not ready')
 return FileResponse(p,media_type='application/zip',filename='viralclip-ai-clips.zip')
@app.get("/", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
def home():
    from fastapi.responses import HTMLResponse

    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ViralClip AI</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#101014;color:white;font-family:Arial;padding:22px}
.container{max-width:600px;margin:auto}
h1{font-size:32px;color:#a78bfa}
p{color:#aaa}
.card{background:#1d1d27;padding:20px;border-radius:16px;margin-top:20px}
input,select{width:100%;padding:14px;margin:10px 0;background:#292936;color:white;border:1px solid #444;border-radius:9px;font-size:16px}
button{width:100%;padding:15px;background:#8b5cf6;color:white;border:0;border-radius:9px;font-size:17px;font-weight:bold;margin-top:12px}
button:disabled{opacity:.5}
video{width:100%;border-radius:10px;margin-top:12px}
a{color:#a78bfa}
#status{margin-top:18px;color:#c4b5fd}
.clip{background:#292936;padding:14px;border-radius:12px;margin-top:15px}
</style>
</head>
<body>
<div class="container">
<h1>⚡ ViralClip AI</h1>
<p>Turn long videos into viral Shorts automatically.</p>

<div class="card">
<h3>🎬 Create your clips</h3>

<form id="clipForm">
<input name="url" type="url" placeholder="Paste YouTube video link" required>

<label>Number of clips</label>
<select name="count">
<option value="20">20 clips</option>
<option value="10">10 clips</option>
<option value="5">5 clips</option>
</select>

<label>Clip duration</label>
<select name="duration">
<option value="30">30 seconds</option>
<option value="15">15 seconds</option>
<option value="60">60 seconds</option>
</select>

<label>Video format</label>
<select name="aspect">
<option value="9:16">9:16 Shorts / Reels</option>
<option value="1:1">1:1 Square</option>
<option value="16:9">16:9 Landscape</option>
</select>

<button id="generate">🚀 Generate Clips</button>
</form>

<div id="status"></div>
<div id="results"></div>
</div>
</div>

<script>
const form=document.getElementById('clipForm');
const statusBox=document.getElementById('status');
const results=document.getElementById('results');
const button=document.getElementById('generate');

form.addEventListener('submit',async(e)=>{
e.preventDefault();
button.disabled=true;
results.innerHTML='';
statusBox.textContent='Starting your AI job...';

try{
const response=await fetch('/api/jobs/youtube',{
method:'POST',
body:new FormData(form)
});
const job=await response.json();
if(!response.ok) throw new Error(job.detail || 'Could not start job');

statusBox.textContent='Job started. Preparing video...';

async function check(){
const r=await fetch('/api/jobs/'+job.job_id);
const data=await r.json();

statusBox.textContent=data.message+' ('+data.progress+'%)';

if(data.status==='failed'){
throw new Error(data.error || 'Processing failed');
}

if(data.status==='ready'){
let clips=typeof data.clips==='string'?JSON.parse(data.clips):data.clips;

results.innerHTML='<h3>🎉 Your clips are ready!</h3>';

clips.forEach(c=>{
const item=document.createElement('div');
item.className='clip';
const title=document.createElement('p');
title.textContent=c.title || 'Highlight';
const video=document.createElement('video');
video.controls=true;
video.src=c.preview;
const link=document.createElement('a');
link.href=c.preview;
link.download=c.file;
link.textContent='⬇ Download clip';
item.append(title,video,document.createElement('br'),link);
results.appendChild(item);
});

const zip=document.createElement('a');
zip.href='/api/jobs/'+job.job_id+'/download';
zip.textContent='⬇ Download all clips (ZIP)';
zip.style.display='block';
zip.style.marginTop='20px';
results.appendChild(zip);

button.disabled=false;
return;
}

setTimeout(()=>check().catch(fail),3000);
}
function fail(error){
statusBox.textContent='Error: '+error.message;
button.disabled=false;
}
check().catch(fail);

}catch(error){
statusBox.textContent='Error: '+error.message;
button.disabled=false;
}
});
</script>
</body>
</html>
""")
 
