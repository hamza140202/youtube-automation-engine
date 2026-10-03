# 🎬 Autonomous YouTube Video Generation Engine (GitHub Actions Cloud Edition)

A 100% automated, zero-cost pipeline designed to generate high-retention, monetizable YouTube Shorts and long-form videos directly in the cloud using **Google AI Pro (Gemini Pro)**, **Edge-TTS Neural Voices**, and **FFmpeg**.

---

## 🚀 How It Works ("Set Up and Sleep on It")

```
   ┌─────────────────────────────────────────────────────────┐
   │ 1. Trigger via GitHub Actions (Dispatch or Daily Cron)  │
   └────────────────────────────┬────────────────────────────┘
                                │
                                ▼
   ┌─────────────────────────────────────────────────────────┐
   │ 2. Script & Visual Cues (Google AI Pro / Fallback)      │
   └────────────────────────────┬────────────────────────────┘
                                │
                                ▼
   ┌─────────────────────────────────────────────────────────┐
   │ 3. Neural Speech & Synchronized Word Subtitles (EdgeTTS)│
   └────────────────────────────┬────────────────────────────┘
                                │
                                ▼
   ┌─────────────────────────────────────────────────────────┐
   │ 4. Kinetic Video Assembly & Dynamic Motion (FFmpeg)     │
   └────────────────────────────┬────────────────────────────┘
                                │
                                ▼
   ┌─────────────────────────────────────────────────────────┐
   │ 5. Auto-Publish to GitHub Releases & Workflow Artifacts │
   └─────────────────────────────────────────────────────────┘
```

---

## ⚡ How to Trigger from GitHub in 1 Click

1. Go to the **Actions** tab in this GitHub repository.
2. Select **"Autonomous YouTube Video Generation Engine"** on the left.
3. Click **"Run workflow"**.
4. Enter any topic (e.g., `Why Humans Can't Remember Being Babies`, `The Mystery of Dark Matter`, `The Psychology of Luck`).
5. Choose **shorts** (9:16) or **landscape** (16:9).
6. Click **Run workflow**.

Within 2 minutes, GitHub Actions will:
* Write the script
* Generate the human-like voiceover
* Animate scene visuals with Ken Burns motion
* Burn high-retention kinetic subtitles
* Package the `.mp4` into a new **GitHub Release** ready to download from your phone or PC!

---

## ⏰ Daily Scheduled Automation
The workflow includes an automated cron schedule:
```yaml
schedule:
  - cron: '0 12 * * *' # Generates a fresh video every day at 12:00 UTC
```
Wake up each morning to a brand new video in your repository's **Releases** tab.

---

## 🔑 Optional Secrets (Settings -> Secrets and variables -> Actions)
* `GEMINI_API_KEY`: For custom Google AI Pro scripts (if omitted, high-retention narrative engine runs automatically).
* `PEXELS_API_KEY`: For downloading live 4K stock video clips (if omitted, high-res atmospheric motion canvases are rendered).
