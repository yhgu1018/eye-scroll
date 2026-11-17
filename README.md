# Eye Scroll

Scroll a long document with your eyes. Look up and the page moves down. Look back to the center and it stops.

No extra hardware. It uses the webcam already sitting above a laptop screen, so you can keep reading without reaching for the mouse or the trackpad.

## What it does

- Opens the built-in camera and tracks both eyes.
- **Look up** to scroll the document **down**.
- **Look down** to scroll the document **up**.
- A steady glance keeps scrolling a little at a time. Look at the center to stop.
- Blinks are ignored, so a normal blink does not jump the page.
- The preview stays on top of other windows and shows which window will receive the scroll.

Laptop cameras sit below your eyes and tilt upward. An upward glance is partly hidden by the eyelid, so the same eye movement looks smaller than a downward one. Eye Scroll boosts the upward signal and leaves the downward one alone. The default sensitivity matches that setup.

## Quick start

Windows, Python 3.11 or newer.

```bat
python -m pip install -r requirements.txt
python eye_scroll.py
```

Or double-click `start.bat`.

1. Look at the **center of the screen**, the way you normally read. Do not stare into the lens. Wait until calibration finishes.
2. Click the document you want to read. Browsers, Word, PDFs, and editors all work if they scroll with the mouse wheel.
3. Look up to move down the page. Look down to move back up.

The first time you run it, the face model is downloaded once into `models/` if it is not already there.

## Controls

| Key | Action |
| --- | --- |
| F8 | Pause or resume |
| F9 | Recalibrate while looking at the center |
| F4 | Scroll the window that is in front right now |
| F7 | Flip up and down |
| F10 | Quit |
| Numpad + / - | Raise or lower sensitivity |

With the preview window focused you can also use Space, C, F, R, Q, and + / -.

Another camera:

```bat
python eye_scroll.py --camera 1
```

## Tips

- Keep both eyes in the frame. If the camera only sees your shirt, it will not scroll.
- The status line shows the target, for example `Scroll: report.docx - Word`. If it still says none, click the document or press F4.
- Sensitivity 78 is the default. Lower it if a small glance scrolls too easily. Raise it if you have to look too far.
- Press F7 if you want the opposite mapping.

## Requirements

- Windows 10 or 11
- A webcam
- Python packages in `requirements.txt`: MediaPipe, OpenCV, Pillow, NumPy

Scrolling is sent as a mouse-wheel event to the document window, which is why this build is Windows-only.

## License

[MIT](LICENSE). The bundled face model is Google's MediaPipe Face Landmarker.
