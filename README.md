# StormWatcher

Drive a camera from a laptop and fire the shutter when lightning is detected.

Two ways to run it:

- **Nikon DSLR on a Windows laptop** — `nikon_lightning.py` (this guide). Built and
  tested against a **Nikon D3300** over USB using digiCamControl.
- **Raspberry Pi + USB webcam** — the original project (`client.py` + `server.py`).
  See [Original Raspberry Pi setup](#original-raspberry-pi--usb-webcam) at the bottom.

---

## ⚠️ Read this first — can you actually photograph the lightning?

It's worth being honest about the physics before you set up, because it changes
how you should shoot:

- A lightning **return stroke lasts ~30 microseconds**; the whole flash (with
  its later strokes) lasts up to a few hundred milliseconds.
- A **"detect-then-shoot" reaction over USB** is *much* slower than that:
  live-view frame interval (~30–100 ms) + image processing + the USB command +
  the D3300's shutter lag (~80–150 ms) ≈ **several hundred ms total**.

So the flash that *triggers* the detector is almost always gone before the
shutter opens. That's not a bug in this code — no software-over-USB trigger can
beat it. What you realistically get from each mode:

| Goal | Use | What you get |
|------|-----|--------------|
| Daytime / very active storms | `MODE = "trigger"` | The instant a flash is seen it shoots. You'll mostly catch **what comes right after** the trigger — later return strokes, the lit-up sky/clouds, the afterglow. Set `BURST = 2–3` to better catch multi-stroke flashes. |
| **Night storms (reliable)** | `MODE = "cull"` | The shutter is kept open in **back-to-back exposures**, so lightning that strikes *during* an exposure is captured. The script then **keeps only the frames that contain lightning** and sets the rest aside. This is how lightning is actually photographed. |
| Truly instant (daytime) | Hardware trigger | A photodiode trigger on the D3300's **MC-DC2** remote port (e.g. MIOPS, Pluto Trigger) fires in microseconds. It's extra hardware, but it's the only thing fast enough to catch the triggering flash itself. |

---

## Nikon D3300 on Windows — setup

### 1. Install digiCamControl
Download and install it from <https://digicamcontrol.com> (free, Windows-only,
the D3300 is supported). Plug the camera in via USB and turn it on — you should
see live controls in digiCamControl.

### 2. Set the camera up for speed
In the camera / digiCamControl:
- **Manual focus**, focused to **infinity**. This is the single biggest speed
  win — no autofocus hunting in the dark. (The script uses *CaptureNoAf* by
  default to match.)
- **Manual exposure mode (M)** with a fixed ISO / shutter / aperture, so the
  camera doesn't pause to meter.
- Shoot **JPEG** (or JPEG+RAW) for fast writes. For `cull` mode you **must**
  have JPEG so the script can read each frame.
- Turn off image review on the camera.

### 3. Enable the web server
`File → Settings → Webserver` → tick **Enable** (port **5513**), then **restart
digiCamControl**. The script talks to it at `http://localhost:5513`, which keeps
the camera session warm so shots fire as fast as possible.

### 4. Install Python deps
Install Python 3 from <https://python.org> (tick *Add to PATH*), then:
```
pip install opencv-python numpy requests
```

### 5. Configure and run
Open `nikon_lightning.py` and edit the `CONFIG` block near the top
(`SAVE_FOLDER`, `MODE`, and the detection thresholds). Then, with digiCamControl
running and the camera connected:
```
python nikon_lightning.py
```
The script opens live view, watches it, and fires the shutter on a flash.
Photos land in your save folder. Press **Ctrl+C** to stop.

### Tuning
- **Too many false shots** → raise `SCORE_TRIGGER`. **Missing obvious flashes**
  → lower it. Watch the `score=...` lines it prints to pick a good value.
- **Multi-stroke flashes** → set `BURST = 2` or `3`.
- Keep `AUTOFOCUS = False` and pre-focus to infinity for the fastest release.
- In `cull` mode, each exposure is judged on its **own** brightness (a bolt is a
  bright cluster against a dark sky), not against other frames. It prints a
  `lit=…%` for every frame — keep too many → raise `CULL_KEEP_PCT`; missing dim
  or distant strikes → lower `CULL_KEEP_PCT` and/or `CULL_BRIGHT_LEVEL`.
- In `cull` mode, rejected frames are moved to a `discard` subfolder by default
  (`CULL_DELETE = False`); set it to `True` to delete them instead.

---

## Original: Raspberry Pi + USB webcam

This is the original project from [the post on
reddit](https://www.reddit.com/r/raspberry_pi/comments/1uabe2z/comment/osmvukx/?screen_view_count=4).
It uses a USB webcam as the detector and a small web server with a live view and
events gallery.

### How to set up:
- Download `client.py` to your client (doesn't have to be a Raspberry Pi, but that is what I used).
- Have a connected USB webcam.
- Find the `/dev/video` node for your USB webcam (and note down the number after video).
- Set this number in `client.py` line 23: `dev = 0`, replace 0 with the number after video.
- Install v4l2 on the client: `sudo apt install v4l-utils`
- Download `server.py` to a suitable server
- Install uvicorn: `pip install uvicorn`
- Run server: `uvicorn server:app --port (YOUR PORT HERE) --host 0.0.0.0`
- Set your server's IP and port in `client.py` line 9: replace `ENTER YOUR API SERVER IP:PORT HERE` with your server ip and your chosen port (previous step).

That's pretty much it.
You can go into a web browser to `http://ip:port` and press `enable stream` and you should see the webcam video, if you do, that means it worked. Enjoy.

<img width="489" height="489" alt="image" src="https://github.com/user-attachments/assets/a498cfa2-4081-419c-bd38-89da32e143eb" />
<img width="489" height="489" alt="image" src="https://github.com/user-attachments/assets/9ce2b221-10d1-4425-befd-da916ce51215" />
<img width="489" height="489" alt="image" src="https://github.com/user-attachments/assets/20cc1dad-39c8-44e2-bbe7-7a3ecb1e4cce" />
