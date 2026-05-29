import os
import time
import yt_dlp
import sys
import mediapipe
from web import app, db, Surfer, SurfVideo, analyze_video_bytes, train_ai

# --- CONFIG ---
DOWNLOAD_FOLDER = "temp_auto_train"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# --- THE DATASET ---
training_data = [
    {
        "url": "https://www.youtube.com/watch?v=UaNMQW74v8E",
        "surfer": "Kelly Slater", "h": 175, "w": 75, "vol": 26.8, "ft": 5, "in": 9.0
    },
    {
        "url": "https://www.youtube.com/shorts/sD16iTluctY",
        "surfer": "Gabriel Medina", "h": 180, "w": 77, "vol": 28.5, "ft": 5, "in": 11.0
    },
    {
        "url": "https://www.youtube.com/shorts/xp086j_iB4Q",
        "surfer": "John John Florence", "h": 185, "w": 82, "vol": 30.5, "ft": 6, "in": 2.0
    },
    {
        "url": "https://www.youtube.com/watch?v=THbIj19TTvI",
        "surfer": "Italo Ferreira", "h": 175, "w": 77, "vol": 25.5, "ft": 5, "in": 7.0
    },
    {
        "url": "https://www.youtube.com/shorts/o2wu6FL4EAo",
        "surfer": "Kelly Slater (Lowers)", "h": 175, "w": 75, "vol": 26.8, "ft": 5, "in": 9.0
    },
    {
        "url": "https://www.youtube.com/shorts/7J251uAGGDg",
        "surfer": "Griffin Colapinto", "h": 180, "w": 78, "vol": 28.5, "ft": 5, "in": 11.0
    },
    {
        "url": "https://www.youtube.com/shorts/PAMssXzucdg",
        "surfer": "Carissa Moore", "h": 170, "w": 64, "vol": 26.5, "ft": 5, "in": 9.0
    },
    {
        "url": "https://www.youtube.com/shorts/OgSeOj_yJMs",
        "surfer": "Stephanie Gilmore", "h": 178, "w": 67, "vol": 24.5, "ft": 5, "in": 10.0
    },
    {
        "url": "https://www.youtube.com/shorts/eS3crQys7G4",
        "surfer": "Kanoa Igarashi", "h": 180, "w": 78, "vol": 29.6, "ft": 6, "in": 0.0
    },
    {
        "url": "https://www.youtube.com/shorts/fUXEm--0kWY",
        "surfer": "Jack Robinson", "h": 180, "w": 81, "vol": 30.0, "ft": 6, "in": 0.0
    }
]

def download_video(url):
    print(f"\n⬇️  Downloading: {url}...")
    
    ydl_opts = {
        'format': 'best[ext=mp4]/best', 
        'outtmpl': f'{DOWNLOAD_FOLDER}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True 
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            if 'entries' in info:
                info = info['entries'][0]
                
            filename = ydl.prepare_filename(info)
            # RETURN JUST THE PATH
            return filename
    except Exception as e:
        print(f"❌ Error downloading: {e}")
        return None

def run_auto_trainer():
    print(f"🚀 STARTING AUTO-TRAINER ON {len(training_data)} CLIPS")
    
    count = 1
    with app.app_context():
        db.create_all()
        for data in training_data:
            print(f"\n[{count}/{len(training_data)}] Processing {data['surfer']}...")
            
            # 1. Download
            path = download_video(data['url'])
            if not path: 
                print("   ⚠️ Skipping (Download failed)")
                continue
            
            # 2. Analyze
            try:
                with open(path, 'rb') as f:
                    vid_bytes = f.read()
                
                score = analyze_video_bytes(vid_bytes)
                print(f"   👉 Motion Score: {score}")
                
                # 3. Save to DB
                s = Surfer(
                    user_email="auto_trainer@surfai.local",
                    height_cm=data['h'],
                    weight_kg=data['w'],
                    skill_level="Expert",
                    video_motion_score=score,
                    rec_liters=data['vol'],
                    rec_feet=data['ft'],
                    rec_inches=data['in'],
                    rec_message="Auto-trained data"
                )
                db.session.add(s)
                db.session.commit()
                
                # Save Video placeholder
                v = SurfVideo(surfer_id=s.id, video_data=b'', motion_score=score)
                db.session.add(v)
                db.session.commit()
                
                print("   ✅ Saved to DB.")
                
            except Exception as e:
                print(f"   ❌ Failed to analyze: {e}")
            
            # Cleanup file
            if path and os.path.exists(path): 
                try:
                    os.remove(path)
                except:
                    pass
            
            count += 1

        print("\n🔄 Re-training AI Models...")
        train_ai()
        print("✨ DONE! System is now trained.")

if __name__ == "__main__":
    run_auto_trainer()