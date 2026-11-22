VIRALCLIP AI — STARTER + BACKEND PROTOTYPE

Frontend: index.html (current mobile-friendly UI prototype)
Backend: backend/main.py (FastAPI), FFmpeg, local faster-whisper transcription.
Setup guide: backend/README.md

The backend prototype supports Google Drive OAuth, allowlisted signed cloud-storage URLs, direct video upload, transcript-based highlight suggestions, MP4 rendering and ZIP packaging.

YouTube URLs are preview-only; no stream extraction or bypassing access controls. For your own YouTube uploads, export/download through YouTube Studio where available, then process the authorized source file from Drive/cloud storage.

Not yet a deployed live service. Deployment needs Google OAuth credentials, secure environment variables, persistent disk, and a backend host. Captions and durable job queue are future iterations.
